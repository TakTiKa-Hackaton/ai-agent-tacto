"""Deterministic offer planning and close decisions using the arena's published cost model."""

from __future__ import annotations

import math

from .cost_model import OPS, RELATIONSHIP_GOALS, complete_offer, must_haves_ok, total_cost
from .document_reader import supplier_standard_terms
from .play import latest, live_threads, moves_left, standing


class OfferPlanner:
    DISCOUNT = {"leverage": 0.07, "routine": 0.04, "quality_first": 0.015, "stabilise": 0.02, "secure_supply": 0.015}

    def __init__(self, researcher):
        self.researcher = researcher

    def supplier_economics(self, case, supplier_id, standard, research):
        buyer = case["buyer"]
        if buyer.get("category_goal") not in RELATIONSHIP_GOALS or len(case.get("parts", [])) != 1:
            return None
        estimate = next((
            row for row in research["should_cost"]
            if row.get("part_id") == case["parts"][0]
            and row.get("supplier_id") == supplier_id
            and row.get("price") is not None
        ), None)
        if not estimate:
            return None
        card = research["scorecards"].get(supplier_id, {})
        return {
            "full_cost": float(estimate["price"]),
            "interest_rate": float(buyer.get("interest_rate", 0)),
            "usual_rate": float(buyer.get("interest_rate", 0)),
            "ppm": float(card.get("ppm") or standard.get("ppm_commitment") or 0),
            "standard": standard,
        }

    def cost(self, case, terms, standard=None, supplier_id=None, research=None):
        economics = self.supplier_economics(case, supplier_id, standard or {}, research or {}) if research else None
        return total_cost(case, terms, standard, supplier=economics)

    def _choices(self, case, threads, excluded_supplier, research):
        choices = []
        alt = case["buyer"].get("alternative") or {}
        alt_terms = alt.get("terms") or {}
        if alt_terms.get("unit_price") is not None and must_haves_ok(case, alt_terms):
            choices.append((self.cost(case, alt_terms, research=research), "documented alternative"))

        fallback = case["buyer"].get("fallback_unit_price")
        if fallback is not None:
            fallback_terms = complete_offer(case, {"unit_price": float(fallback)})
            if must_haves_ok(case, fallback_terms):
                choices.append((self.cost(case, fallback_terms, research=research), "fallback supply"))

        for sid, events in threads.items():
            if sid == excluded_supplier or sid not in live_threads({sid: events}):
                continue
            sheet_id, terms = standing(events)
            if not sheet_id or terms.get("unit_price") is None:
                continue
            standard = supplier_standard_terms(events)
            if must_haves_ok(case, terms, standard):
                choices.append((self.cost(case, terms, standard, sid, research), f"live quote {sid}"))
        return choices

    def batna(self, case, threads, excluded_supplier, research):
        choices = self._choices(case, threads, excluded_supplier, research)
        return min(choices, default=(math.inf, "no priced alternative"), key=lambda choice: choice[0])

    def target_price(self, case, supplier_id, terms, research):
        buyer = case["buyer"]
        baseline = buyer.get("current_unit_price")
        if baseline is None:
            alternative = buyer.get("alternative") or {}
            baseline = (alternative.get("terms") or {}).get("unit_price")
        if baseline is None:
            history = [row for row in research["purchase_history"] if row.get("supplier_id") == supplier_id]
            history.sort(key=lambda row: row.get("date", ""), reverse=True)
            baseline = history[0].get("price") if history else None
        if baseline is None:
            history = research["purchase_history"]
            history.sort(key=lambda row: row.get("date", ""), reverse=True)
            baseline = history[0].get("price") if history else None
        if baseline is None:
            baseline = buyer.get("fallback_unit_price") or (terms or {}).get("unit_price") or 1.0

        fair = float(baseline) * (1 + self.researcher.weighted_market_change(research) / 100)
        category = buyer.get("category_goal", "routine")
        target = fair * (1 - self.DISCOUNT.get(category, 0.04))

        # A current, same-part competitor quote is a stronger ceiling than a general index.
        recent_quotes = [
            row for row in research["quotes"]
            if row.get("supplier_id") != supplier_id and row.get("price") is not None
        ]
        recent_quotes.sort(key=lambda row: row.get("date", ""), reverse=True)
        if recent_quotes:
            target = min(target, float(recent_quotes[0]["price"]))

        if category in RELATIONSHIP_GOALS and len(case.get("parts", [])) == 1:
            estimate = next((
                row for row in research["should_cost"]
                if row.get("part_id") == case["parts"][0]
                and row.get("supplier_id") == supplier_id and row.get("price") is not None
            ), None)
            if estimate:
                sustainable = max(0.0, min(80.0, float(buyer.get("sustainable_margin_pct") or 0)))
                target = max(target, float(estimate["price"]) / max(0.2, 1 - sustainable / 100))
        return round(max(0.01, target), 2)

    def _optimize_terms(self, case, terms, standard, supplier_id, research):
        options = case.get("term_options", {})
        for _ in range(2):
            moved = False
            for field in case.get("negotiable", []):
                values = options.get(field, [])
                if field == "unit_price" or not values:
                    continue
                musts = [item for item in case["buyer"].get("must_haves", []) if item.get("field") == field]
                candidates = [value for value in values if all(OPS[item["op"]](value, item["value"]) for item in musts)]
                scored = []
                for value in candidates:
                    candidate = complete_offer(case, {**terms, field: value})
                    if must_haves_ok(case, candidate, standard):
                        scored.append((self.cost(case, candidate, standard, supplier_id, research), repr(value), candidate))
                if scored:
                    best = min(scored, key=lambda item: (item[0], item[1]))[2]
                    if terms.get(field) != best.get(field):
                        terms, moved = best, True
            if not moved:
                break
        return complete_offer(case, terms)

    def _max_price(self, case, template, standard, supplier_id, cap, research):
        if not math.isfinite(cap):
            return float(template.get("unit_price") or 0)
        low, high = 0.01, max(10.0, float(template.get("unit_price") or 1) * 8)
        for _ in range(48):
            mid = (low + high) / 2
            candidate = complete_offer(case, {**template, "unit_price": mid})
            if self.cost(case, candidate, standard, supplier_id, research) <= cap + 0.01:
                low = mid
            else:
                high = mid
        return round(low, 2)

    def _within_cap(self, case, terms, standard, sid, cap, research):
        terms = complete_offer(case, terms)
        if self.cost(case, terms, standard, sid, research) <= cap + 0.01:
            return terms
        ceiling = self._max_price(case, terms, standard, sid, cap, research)
        terms["unit_price"] = min(float(terms.get("unit_price") or ceiling), ceiling)
        return complete_offer(case, terms)

    def _packages(self, case, primary, standard, sid, cap, research):
        primary = self._within_cap(case, primary, standard, sid, cap, research)
        packages = []
        if (must_haves_ok(case, primary, standard)
                and self.cost(case, primary, standard, sid, research) <= cap + 0.01):
            packages.append(primary)
        options, allowed = case.get("term_options", {}), set(case.get("negotiable", []))
        musts = case["buyer"].get("must_haves", [])
        uplift = max(0.01, round(float(primary.get("unit_price") or 0) * 0.01, 2))

        def add(candidate):
            candidate = self._within_cap(case, candidate, standard, sid, cap, research)
            if (must_haves_ok(case, candidate, standard)
                    and self.cost(case, candidate, standard, sid, research) <= cap + 0.01
                    and candidate not in packages and len(packages) < 3):
                packages.append(candidate)

        # Show a small set of explicit trades: faster cash or a longer commitment for a price premium.
        for field in ("payment_days", "contract_months"):
            values = options.get(field, [])
            if field not in allowed or not values or len(packages) >= 3:
                continue
            valid = [value for value in values if all(
                OPS[must["op"]](value, must["value"]) for must in musts if must.get("field") == field
            )]
            if not valid:
                continue
            seller_value = min(valid) if field == "payment_days" else max(valid)
            if seller_value == primary.get(field):
                continue
            variant = {**primary, field: seller_value, "unit_price": float(primary.get("unit_price") or 0) + uplift}
            add(variant)
        return packages

    def _state(self, case, sid, events, research):
        sheet_id, offered = standing(events)
        standard = supplier_standard_terms(events)
        terms = complete_offer(case, offered) if offered.get("unit_price") is not None else {}
        valid = bool(terms) and must_haves_ok(case, terms, standard)
        cost = self.cost(case, terms, standard, sid, research) if valid else math.inf
        target = self.target_price(case, sid, terms, research)
        goal_terms = self._optimize_terms(case, complete_offer(case, {**terms, "unit_price": target}), standard, sid, research)
        target_cost = self.cost(case, goal_terms, standard, sid, research)
        return {
            "sheet_id": sheet_id, "terms": terms, "valid": valid, "cost": cost,
            "standard": standard, "target": target, "goal_terms": goal_terms, "target_cost": target_cost,
            "status": latest(events).get("status"), "turn": sum(m.get("from") == "buyer" for m in events),
        }

    def decide(self, case, threads, research, writer):
        live = live_threads(threads)
        if not live:
            raise ValueError("cannot decide after all supplier threads have ended")
        states = {sid: self._state(case, sid, threads[sid], research) for sid in live}
        caps = {
            sid: self.batna(case, {other: events for other, events in threads.items() if other != sid}, sid, research)[0]
            for sid in live
        }

        ready = []
        for sid, state in states.items():
            terminal = state["status"] in {"final_offer", "agreed"} or moves_left(case, threads[sid]) <= 0
            if (state["valid"] and state["sheet_id"] and state["cost"] <= caps[sid] + 0.01
                    and (state["cost"] <= state["target_cost"] + 0.01 or terminal)):
                ready.append((state["cost"], sid, state["sheet_id"]))
        if ready:
            _, sid, offer_id = min(ready)
            return {"supplier_id": sid, "message": {"action": {"type": "accept", "term_sheet_id": offer_id}}}

        writable = [sid for sid in live if moves_left(case, threads[sid]) > 0
                    and states[sid]["status"] not in {"final_offer", "agreed"}]
        if not writable:
            return {"supplier_id": live[0], "message": {"action": {"type": "walk_away"}}}

        # Work the best priced supplier first; if no supplier has quoted yet, pursue the best researched target.
        quoted = [sid for sid in writable if states[sid]["valid"]]
        sid = min(quoted, key=lambda item: (states[item]["cost"], item)) if quoted else min(
            writable, key=lambda item: (states[item]["target_cost"], item)
        )
        state, standard, cap = states[sid], states[sid]["standard"], caps[sid]
        history = [m for m in threads[sid] if m.get("from") == "buyer" and m.get("term_sheets")]
        if history:
            previous = history[-1]["term_sheets"][0]["terms"].get("unit_price", state["target"])
            remaining = max(1, min(5, case.get("round_limit", 10) - 1 - state["turn"]))
            planned_price = float(previous) + (state["target"] - float(previous)) / remaining
            planned_price = min(state["target"], planned_price)
        else:
            planned_price = state["target"] * 0.92
        template = state["terms"] or {}
        primary = self._optimize_terms(
            case, complete_offer(case, {**template, "unit_price": max(0.01, planned_price)}),
            standard, sid, research,
        )
        packages = self._packages(case, primary, standard, sid, cap, research)
        evidence_line, refs = self.researcher.opening_evidence(case, research)
        draft = writer.draft(case, latest(threads[sid]), evidence_line, bool(packages))
        # Verified evidence is inserted by code; the language model is not allowed to alter it.
        body = evidence_line + "\n\n" + draft
        return {
            "supplier_id": sid,
            "message": {
                "subject": f"Re: {case['title']}"[:300],
                "body": body[:8000],
                "term_sheets": [{"terms": package} for package in packages],
                "refs": refs[:20],
            },
        }
