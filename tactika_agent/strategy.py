"""Deterministic offer planning and close decisions using the arena's published cost model."""

from __future__ import annotations

import json
import math
from datetime import date, timedelta
from itertools import product

from .council import FIELD_OWNERS
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
        today = date.today()
        estimate = next((
            row for row in research["should_cost"]
            if row.get("part_id") == case["parts"][0]
            and row.get("supplier_id") == supplier_id
            and row.get("price") is not None
            and float(row.get("confidence_pct") or 0) >= 65
            and self._evidence_is_fresh(row, today, 1095)
        ), None)
        if not estimate:
            return None
        card = research["scorecards"].get(supplier_id, {})
        return {
            "full_cost": float(estimate["price"]),
            # Supplier financing cost is not the buyer's financing rate. Use a
            # supplier-specific value only when the case explicitly provides one.
            "interest_rate": float(buyer.get("supplier_interest_rate", 0)),
            "usual_rate": float(buyer.get("supplier_interest_rate", 0)),
            "ppm": float(card.get("ppm") or standard.get("ppm_commitment") or 0),
            "standard": standard,
        }

    def cost(self, case, terms, standard=None, supplier_id=None, research=None):
        economics = self.supplier_economics(case, supplier_id, standard or {}, research or {}) if research else None
        return total_cost(case, terms, standard, supplier=economics)

    @staticmethod
    def _evidence_is_fresh(row, today, max_age_days):
        try:
            age = (today - date.fromisoformat(row["date"])).days
        except (KeyError, TypeError, ValueError):
            return False
        return 0 <= age <= max_age_days

    @staticmethod
    def _quote_is_comparable(case, quote):
        normalized = {**quote, "unit_price": quote.get("unit_price", quote.get("price"))}
        fields = {
            "volume_per_year", "contract_months", "payment_days", "lead_time_days",
            "incoterm", "ppm_commitment", "chargeback_eur_per_complaint", "tooling_eur",
            "effective_date", "index_clause",
        }
        expected = {**complete_offer(case, {}), **case.get("fixed_terms", {})}
        negotiable = set(case.get("negotiable", []))
        for field in fields:
            if field in negotiable and normalized.get(field) is None:
                return False
            if (normalized.get(field) is not None and expected.get(field) is not None
                    and normalized[field] != expected[field]):
                return False
        rules = case["buyer"].get("must_haves", [])
        if any(rule.get("field") != "unit_price" and normalized.get(rule.get("field")) is None
               for rule in rules):
            return False
        return must_haves_ok(case, normalized)

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
            case_parts = case.get("parts", [])
            history = [
                row for row in research["purchase_history"]
                if row.get("supplier_id") == supplier_id
                and len(case_parts) == 1 and row.get("part_id") == case_parts[0]
                and self._evidence_is_fresh(row, date.today(), 1095)
            ]
            history.sort(key=lambda row: row.get("date", ""), reverse=True)
            baseline = history[0].get("price") if history else None
        if baseline is None:
            case_parts = case.get("parts", [])
            history = [
                row for row in research["purchase_history"]
                if len(case_parts) == 1 and row.get("part_id") == case_parts[0]
                and self._evidence_is_fresh(row, date.today(), 1095)
            ]
            history.sort(key=lambda row: row.get("date", ""), reverse=True)
            baseline = history[0].get("price") if history else None
        if baseline is None:
            baseline = buyer.get("fallback_unit_price") or (terms or {}).get("unit_price") or 1.0

        fair = float(baseline) * (1 + self.researcher.weighted_market_change(research) / 100)
        category = buyer.get("category_goal", "routine")
        target = fair * (1 - self.DISCOUNT.get(category, 0.04))

        # Only a live awarded quote with buyer-compatible terms is a reliable price ceiling.
        case_parts = case.get("parts", [])
        freight = buyer["freight_eur_per_unit"]
        preferred_incoterm = min(freight, key=freight.get)
        today = date.today()
        recent_quotes = [
            row for row in research["quotes"]
            if len(case_parts) == 1 and row.get("part_id") == case_parts[0]
            and row.get("supplier_id") != supplier_id and row.get("price") is not None
            and row.get("awarded") in (True, 1, "true", "TRUE")
            and row.get("payment_days") == buyer.get("reference_payment_days", 0)
            and row.get("lead_time_days") == buyer.get("need_lead_time_days")
            and row.get("incoterm") == preferred_incoterm
            and not (row.get("tooling_eur") or 0)
            and self._quote_is_valid(row, today)
            and self._quote_is_comparable(case, row)
        ]
        if recent_quotes:
            target = min(target, min(float(row["price"]) for row in recent_quotes))

        if category in RELATIONSHIP_GOALS and len(case.get("parts", [])) == 1:
            estimate = next((
                row for row in research["should_cost"]
                if row.get("part_id") == case["parts"][0]
                and row.get("supplier_id") == supplier_id and row.get("price") is not None
                and float(row.get("confidence_pct") or 0) >= 65
                and self._evidence_is_fresh(row, today, 1095)
            ), None)
            if estimate:
                sustainable = max(0.0, min(80.0, float(buyer.get("sustainable_margin_pct") or 0)))
                target = max(target, float(estimate["price"]) / max(0.2, 1 - sustainable / 100))
        return round(max(0.01, target), 2)

    @staticmethod
    def _quote_is_valid(quote, today):
        try:
            quoted = date.fromisoformat(quote["date"])
            expires = quoted + timedelta(days=int(quote["validity_days"]))
        except (KeyError, TypeError, ValueError):
            return False
        return quoted <= today <= expires

    def _optimize_terms(self, case, terms, standard, supplier_id, research):
        options = case.get("term_options", {})
        fields = [
            field for field in case.get("negotiable", [])
            if field != "unit_price" and options.get(field)
        ]
        if not fields:
            return complete_offer(case, terms)

        best_key, best_terms = None, None
        for values in product(*(options[field] for field in fields)):
            candidate = complete_offer(case, {**terms, **dict(zip(fields, values))})
            if not must_haves_ok(case, candidate, standard):
                continue
            key = (self.cost(case, candidate, standard, supplier_id, research), tuple(map(repr, values)))
            if best_key is None or key < best_key:
                best_key, best_terms = key, candidate
        return best_terms if best_terms is not None else complete_offer(case, terms)

    def _max_price(self, case, template, standard, supplier_id, cap, research):
        if not math.isfinite(cap):
            return float(template.get("unit_price") or 0)
        choices = case.get("term_options", {}).get("unit_price", [])
        if choices:
            feasible = [
                float(price) for price in choices
                if self.cost(case, complete_offer(case, {**template, "unit_price": price}),
                             standard, supplier_id, research) <= cap + 0.01
            ]
            return max(feasible, default=None)

        low, high = 0.01, max(10.0, float(template.get("unit_price") or 1) * 8)
        steps = 256
        best = None
        template_price = template.get("unit_price")
        if template_price is not None:
            candidate = complete_offer(case, {**template, "unit_price": template_price})
            if self.cost(case, candidate, standard, supplier_id, research) <= cap + 0.01:
                best = float(candidate["unit_price"])
        previous_price = low
        previous_ok = self.cost(
            case, complete_offer(case, {**template, "unit_price": low}), standard, supplier_id, research
        ) <= cap + 0.01
        if previous_ok:
            best = low
        for step in range(1, steps + 1):
            price = low + (high - low) * step / steps
            candidate = complete_offer(case, {**template, "unit_price": price})
            feasible = self.cost(case, candidate, standard, supplier_id, research) <= cap + 0.01
            if feasible:
                best = price
            elif previous_ok:
                left, right = previous_price, price
                for _ in range(32):
                    mid = (left + right) / 2
                    probe = complete_offer(case, {**template, "unit_price": mid})
                    if self.cost(case, probe, standard, supplier_id, research) <= cap + 0.01:
                        left = mid
                    else:
                        right = mid
                best = max(best or 0.0, left)
            previous_price, previous_ok = price, feasible
        return round(best, 2) if best is not None else None

    def _within_cap(self, case, terms, standard, sid, cap, research):
        terms = complete_offer(case, terms)
        if self.cost(case, terms, standard, sid, research) <= cap + 0.01:
            return terms
        ceiling = self._max_price(case, terms, standard, sid, cap, research)
        if ceiling is None:
            return None
        terms["unit_price"] = min(float(terms.get("unit_price") or ceiling), ceiling)
        terms = complete_offer(case, terms)
        return terms if self.cost(case, terms, standard, sid, research) <= cap + 0.01 else None

    def _packages(self, case, primary, standard, sid, cap, research):
        primary = self._within_cap(case, primary, standard, sid, cap, research)
        if primary is None:
            return []
        packages = []
        primary_cost = self.cost(case, primary, standard, sid, research)
        if must_haves_ok(case, primary, standard) and primary_cost <= cap + 0.01:
            packages.append(primary)
        package_cap = min(cap, primary_cost + max(0.01, abs(primary_cost) * 0.001))
        options, allowed = case.get("term_options", {}), set(case.get("negotiable", []))
        musts = case["buyer"].get("must_haves", [])
        seller_directions = {
            "payment_days": "lower",
            "contract_months": "higher",
            "volume_per_year": "higher",
            "lead_time_days": "higher",
            "ppm_commitment": "higher",
            "chargeback_eur_per_complaint": "lower",
        }
        variants = []

        def add(candidate, seller_value):
            candidate = complete_offer(case, candidate)
            if (must_haves_ok(case, candidate, standard)
                    and self.cost(case, candidate, standard, sid, research) <= package_cap + 0.01
                    and candidate not in packages
                    and all(candidate != item[1] for item in variants)):
                variants.append((seller_value, candidate))

        # Offer one-issue alternatives that are conventionally attractive to a
        # supplier, then compensate for the term change with the highest buyer-safe
        # price. The cost model and must-haves remain the hard filters.
        for field, direction in seller_directions.items():
            values = options.get(field, [])
            if field not in allowed or not values:
                continue
            baseline = standard.get(field)
            if baseline is None:
                baseline = primary.get(field)
            numeric_options = [float(item) for item in values]
            span = max(numeric_options) - min(numeric_options) if numeric_options else 0.0
            scale = span or 1.0
            for value in values:
                if value == primary.get(field):
                    continue
                if not all(OPS[must["op"]](value, must["value"])
                           for must in musts if must.get("field") == field):
                    continue
                if direction == "lower":
                    advantage = (float(baseline) - float(value)) / scale
                else:
                    advantage = (float(value) - float(baseline)) / scale
                advantage = min(1.0, advantage)
                if advantage <= 0:
                    continue
                variant = {**primary, field: value}
                max_price = self._max_price(
                    case, variant, standard, sid, package_cap, research
                )
                if max_price is None:
                    continue
                variant["unit_price"] = max_price
                add(variant, advantage)

        variants.sort(key=lambda item: (-item[0], self.cost(case, item[1], standard, sid, research)))
        packages.extend(candidate for _, candidate in variants[:max(0, 3 - len(packages))])
        return packages

    def _state(self, case, sid, events, research):
        sheet_id, offered = standing(events)
        standard = supplier_standard_terms(events)
        terms = dict(offered) if offered.get("unit_price") is not None else {}
        valid = terms.get("unit_price") is not None and must_haves_ok(case, terms, standard)
        cost = self.cost(case, terms, standard, sid, research) if valid else math.inf
        target = self.target_price(case, sid, terms, research)
        goal_terms = self._optimize_terms(case, complete_offer(case, {**terms, "unit_price": target}), standard, sid, research)
        target_cost = self.cost(case, goal_terms, standard, sid, research)
        return {
            "sheet_id": sheet_id, "terms": terms, "valid": valid, "cost": cost,
            "standard": standard, "target": target, "goal_terms": goal_terms, "target_cost": target_cost,
            "status": latest(events).get("status"), "turn": sum(m.get("from") == "buyer" for m in events),
        }

    def _earned_cap(self, case, events, buyer_history, standard, supplier_id, batna_cap, research):
        """Permit a bounded concession only after a fresh supplier improvement."""
        if not buyer_history:
            return batna_cap

        previous_costs = [
            self.cost(case, complete_offer(case, sheet["terms"]), standard, supplier_id, research)
            for sheet in buyer_history[-1].get("term_sheets", [])
            if sheet.get("terms") and sheet["terms"].get("unit_price") is not None
        ]
        if not previous_costs:
            return batna_cap
        previous_buyer_cost = min(previous_costs)

        # Track distinct supplier replies. A new but unchanged or worse counteroffer
        # must not buy the supplier more headroom.
        latest_buyer_index = max(
            (index for index, message in enumerate(events)
             if message.get("from") == "buyer" and message.get("term_sheets")),
            default=-1,
        )
        if latest_buyer_index < 0:
            return min(batna_cap, previous_buyer_cost)
        supplier_replies = []
        seen_replies = set()
        for index, message in enumerate(events):
            if message.get("from") == "buyer":
                continue
            sheets = [sheet for sheet in message.get("term_sheets", [])
                      if sheet.get("terms") and sheet["terms"].get("unit_price") is not None]
            if not sheets:
                continue
            fingerprint = json.dumps(
                sorted(json.dumps(sheet["terms"], sort_keys=True, separators=(",", ":"), default=str)
                       for sheet in sheets),
                separators=(",", ":"),
            )
            if fingerprint in seen_replies:
                continue
            seen_replies.add(fingerprint)
            costs = [self.cost(case, complete_offer(case, sheet["terms"]),
                               standard, supplier_id, research) for sheet in sheets]
            supplier_replies.append((index, min(costs)))
        if (not supplier_replies or not events or events[-1].get("from") == "buyer"
                or supplier_replies[-1][0] <= latest_buyer_index):
            return min(batna_cap, previous_buyer_cost)
        # One fresh, distinct counteroffer is not enough by itself: it must improve
        # on the supplier's previous priced response in buyer-cost terms.
        if (len(supplier_replies) < 2
                or supplier_replies[-1][1] >= supplier_replies[-2][1] - 0.01):
            return min(batna_cap, previous_buyer_cost)

        headroom = max(0.0, batna_cap - previous_buyer_cost)
        remaining = max(1, min(5, case.get("round_limit", 10) - len(buyer_history)))
        step = headroom / remaining
        return min(batna_cap, previous_buyer_cost + step)

    def offer_ceiling(self, case, threads, supplier_id, research):
        """Return this turn's earned, BATNA-safe cost ceiling for one supplier."""
        events = threads[supplier_id]
        standard = supplier_standard_terms(events)
        batna_cap = self.batna(
            case, {other: messages for other, messages in threads.items() if other != supplier_id},
            supplier_id, research,
        )[0]
        history = [message for message in events
                   if message.get("from") == "buyer" and message.get("term_sheets")]
        return self._earned_cap(case, events, history, standard, supplier_id, batna_cap, research)

    def council_packages(self, case, threads, supplier_id, research, baseline, suggestions):
        """Add specialist terms that stay within the planner's existing earned-cost limit."""
        if not baseline:
            return []
        allowed = set(case.get("negotiable", []))
        options = case.get("term_options", {})
        events = threads[supplier_id]
        standard = supplier_standard_terms(events)
        cap = self.offer_ceiling(case, threads, supplier_id, research)
        primary = complete_offer(case, baseline[0].get("terms", {}))
        primary_cost = self.cost(case, primary, standard, supplier_id, research)
        package_cap = min(cap, primary_cost + max(0.01, abs(primary_cost) * 0.001))
        packages = [complete_offer(case, item["terms"]) for item in baseline]

        def add(candidate):
            try:
                candidate = self._within_cap(
                    case, candidate, standard, supplier_id, package_cap, research
                )
            except (KeyError, TypeError, ValueError, OverflowError):
                return
            if (candidate is not None and set(candidate) == allowed
                    and must_haves_ok(case, candidate, standard)
                    and self.cost(case, candidate, standard, supplier_id, research) <= package_cap + 0.01
                    and candidate not in packages and len(packages) < 12):
                packages.append(candidate)

        combined = dict(primary)
        for role, terms in suggestions.items():
            if role not in FIELD_OWNERS or not isinstance(terms, dict):
                continue
            if not set(terms) <= FIELD_OWNERS[role] or not set(terms) <= allowed:
                continue
            if not self._valid_advisory_terms(terms):
                continue
            if any(field in options and value not in options[field] for field, value in terms.items()):
                continue
            add(complete_offer(case, {**primary, **terms}))
            combined.update(terms)
        if len(suggestions) > 1:
            add(complete_offer(case, combined))
        return packages[:12]

    @staticmethod
    def _valid_advisory_terms(terms):
        for field, value in terms.items():
            if field == "index_clause":
                if (not isinstance(value, dict)
                        or set(value) != {"series", "material_share", "deadband_pct", "base"}
                        or not isinstance(value["series"], str) or not value["series"]
                        or not isinstance(value["base"], str)
                        or isinstance(value["material_share"], bool)
                        or not isinstance(value["material_share"], (int, float))
                        or not math.isfinite(value["material_share"])
                        or not 0 <= value["material_share"] <= 1
                        or isinstance(value["deadband_pct"], bool)
                        or not isinstance(value["deadband_pct"], (int, float))
                        or not math.isfinite(value["deadband_pct"])
                        or not 0 <= value["deadband_pct"] <= 100):
                    return False
                try:
                    date.fromisoformat(value["base"] + "-01")
                except ValueError:
                    return False
            elif field in {"unit_price", "tooling_eur", "chargeback_eur_per_complaint"}:
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value < 0
                        or (field == "unit_price" and value == 0)):
                    return False
            elif field in {"payment_days", "lead_time_days", "volume_per_year", "contract_months",
                           "ppm_commitment"}:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    return False
            elif field == "effective_date":
                try:
                    date.fromisoformat(value)
                except (TypeError, ValueError):
                    return False
            elif field == "incoterm" and not isinstance(value, str):
                return False
        return True

    def is_safe_specialist_candidate(self, case, threads, supplier_id, terms, research, baseline):
        """Recheck candidate schema, permitted values, must-haves, and the current limit."""
        allowed = set(case.get("negotiable", []))
        if set(terms) != allowed or terms.get("unit_price") is None:
            return False
        baseline_terms = baseline[0]["terms"] if baseline else {}
        changed_terms = {field: value for field, value in terms.items()
                         if value != baseline_terms.get(field)}
        if not self._valid_advisory_terms(changed_terms):
            return False
        options = case.get("term_options", {})
        if any(field in options and value not in options[field] for field, value in terms.items()):
            return False
        if ("incoterm" in changed_terms
                and changed_terms["incoterm"] not in case["buyer"].get("freight_eur_per_unit", {})):
            return False
        events = threads[supplier_id]
        standard = supplier_standard_terms(events)
        if not must_haves_ok(case, terms, standard):
            return False
        cost = self.cost(case, terms, standard, supplier_id, research)
        cap = self.offer_ceiling(case, threads, supplier_id, research)
        baseline_costs = [
            self.cost(case, sheet["terms"], standard, supplier_id, research)
            for sheet in baseline
        ]
        package_cap = min(cap, min(baseline_costs, default=math.inf)
                           + max(0.01, abs(min(baseline_costs, default=0.0)) * 0.001))
        return math.isfinite(cost) and cost <= package_cap + 0.01

    def close_decision(self, case, threads, research):
        """At a deadline, accept the best currently safe quote or close one thread."""
        live = live_threads(threads)
        if not live:
            raise ValueError("cannot close after all supplier threads have ended")
        choices = []
        for sid in live:
            state = self._state(case, sid, threads[sid], research)
            cap = self.batna(
                case, {other: events for other, events in threads.items() if other != sid},
                sid, research,
            )[0]
            if (state["valid"] and state["sheet_id"] and math.isfinite(state["cost"])
                    and state["cost"] <= (cap if math.isfinite(cap) else state["target_cost"]) + 0.01):
                choices.append((state["cost"], sid, state["sheet_id"]))
        if choices:
            _, sid, sheet_id = min(choices)
            return {"supplier_id": sid,
                    "message": {"action": {"type": "accept", "term_sheet_id": sheet_id}}}
        # Close one thread at a time. The session runner refreshes state and repeats
        # this decision while other suppliers remain active.
        sid = max(live, key=lambda item: (self._state(case, item, threads[item], research)["cost"], item))
        return {"supplier_id": sid, "message": {"action": {"type": "walk_away"}}}

    def acceptance_threshold(self, case, events, state, batna_cap):
        """Return the best buyer cost acceptable now, relaxing toward BATNA over time."""
        # Without a priced alternative, a terminal label must not turn infinity into
        # an unlimited reservation price. Keep the evidence-based internal target.
        if not math.isfinite(batna_cap):
            return state["target_cost"]
        terminal = state["status"] in {"final_offer", "agreed"} or moves_left(case, events) <= 0
        if terminal:
            return batna_cap
        total_turns = max(1, case.get("round_limit", 10))
        elapsed = min(1.0, max(0, total_turns - moves_left(case, events)) / total_turns)
        target = min(state["target_cost"], batna_cap)
        return target + (batna_cap - target) * elapsed**2

    def offer_packages(self, case, threads, sid, research):
        """Create the current BATNA-safe package set without drafting email prose."""
        events = threads[sid]
        if moves_left(case, events) <= 0 or latest(events).get("status") in {"final_offer", "agreed"}:
            return []
        state = self._state(case, sid, events, research)
        standard = state["standard"]
        cap = self.batna(
            case, {other: messages for other, messages in threads.items() if other != sid},
            sid, research,
        )[0]
        history = [message for message in events if message.get("from") == "buyer" and message.get("term_sheets")]
        offer_cap = self._earned_cap(case, events, history, standard, sid, cap, research)
        if history:
            previous = history[-1]["term_sheets"][0]["terms"].get("unit_price", state["target"])
            remaining = max(1, min(5, case.get("round_limit", 10) - 1 - state["turn"]))
            planned_price = float(previous) + (state["target"] - float(previous)) / remaining
            planned_price = min(state["target"], planned_price)
        else:
            planned_price = state["target"] * 0.96
        template = state["terms"] or {}
        primary = self._optimize_terms(
            case, complete_offer(case, {**template, "unit_price": max(0.01, planned_price)}),
            standard, sid, research,
        )
        return self._packages(case, primary, standard, sid, offer_cap, research)

    def _untried_packages(self, packages, events):
        sent = {
            json.dumps(sheet.get("terms", {}), sort_keys=True, separators=(",", ":"), default=str)
            for message in events if message.get("from") == "buyer"
            for sheet in message.get("term_sheets", [])
        }
        return [
            package for package in packages
            if json.dumps(package, sort_keys=True, separators=(",", ":"), default=str) not in sent
        ]

    def _improving_untried_package(self, case, threads, supplier_id, research, state):
        events = threads[supplier_id]
        if (state["status"] in {"final_offer", "agreed"}
                or moves_left(case, events) <= 0 or not state["valid"]):
            return None
        minimum_gain = max(0.01, abs(state["cost"]) * 0.005)
        for package in self._untried_packages(
                self.offer_packages(case, threads, supplier_id, research), events):
            if self.cost(case, package, state["standard"], supplier_id, research) < state["cost"] - minimum_gain:
                return package
        return None

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
            accepted_offer = state["status"] == "agreed"
            if (state["valid"] and state["sheet_id"] and state["cost"] <= caps[sid] + 0.01
                    and (accepted_offer or state["cost"] <= self.acceptance_threshold(
                        case, threads[sid], state, caps[sid]
                    ) + 0.01)):
                ready.append((state["cost"], sid, state["sheet_id"]))
        deferred = None
        if ready:
            best_cost, best_sid, offer_id = min(ready)
            # A good standing quote is a real alternative, but do not close while a
            # live supplier has a materially better, BATNA-safe target and time to answer.
            opportunities = []
            if (moves_left(case, threads[best_sid]) > 0
                    and states[best_sid]["status"] != "final_offer"):
                opportunities = sorted(
                    (states[sid]["target_cost"], sid) for sid in live
                    if sid != best_sid and moves_left(case, threads[sid]) > 0
                    and states[sid]["status"] != "final_offer"
                    and states[sid]["target_cost"] < best_cost - max(0.01, abs(best_cost) * 0.02)
                )
            if opportunities:
                _, other_sid = opportunities[0]
                candidate_packages = self._untried_packages(
                    self.offer_packages(case, threads, other_sid, research), threads[other_sid]
                )
                if candidate_packages:
                    deferred = (other_sid, candidate_packages)
            if deferred is None:
                improvement = self._improving_untried_package(
                    case, threads, best_sid, research, states[best_sid]
                )
                if improvement is not None:
                    deferred = (best_sid, [improvement])
            if deferred is None:
                return {"supplier_id": best_sid,
                        "message": {"action": {"type": "accept", "term_sheet_id": offer_id}}}

        writable = [sid for sid in live if moves_left(case, threads[sid]) > 0
                    and states[sid]["status"] not in {"final_offer", "agreed"}]
        if not writable:
            return {"supplier_id": live[0], "message": {"action": {"type": "walk_away"}}}

        # Work the best priced supplier first, then try the remaining open suppliers if its
        # discrete terms cannot produce any BATNA-safe offer.
        quoted = [sid for sid in writable if states[sid]["valid"]]
        offer_order = sorted(quoted, key=lambda item: (states[item]["cost"], item))
        offer_order += sorted(
            (sid for sid in writable if sid not in quoted),
            key=lambda item: (states[item]["target_cost"], item),
        )
        sid, packages = deferred if deferred is not None else (None, [])
        if deferred is None:
            for candidate_sid in offer_order:
                candidate_packages = self._untried_packages(
                    self.offer_packages(case, threads, candidate_sid, research), threads[candidate_sid]
                )
                if candidate_packages:
                    sid, packages = candidate_sid, candidate_packages
                    break
        if not packages:
            inquiry_supplier = next((
                candidate_sid for candidate_sid in offer_order
                if states[candidate_sid]["valid"]
                and not any(message.get("from") == "buyer" and not message.get("term_sheets")
                            and not message.get("action") for message in threads[candidate_sid])
            ), None)
            if inquiry_supplier is not None:
                evidence_line, refs = self.researcher.opening_evidence(case, research)
                draft = writer.draft(case, latest(threads[inquiry_supplier]), evidence_line, [],
                                     history=threads[inquiry_supplier][-6:])
                return {
                    "supplier_id": inquiry_supplier,
                    "message": {
                        "subject": f"Re: {case['title']}"[:300],
                        "body": (evidence_line + "\n\n" + draft)[:8000],
                        "term_sheets": [],
                        "refs": refs[:20],
                    },
                }
            return {"supplier_id": live[0], "message": {"action": {"type": "walk_away"}}}
        evidence_line, refs = self.researcher.opening_evidence(case, research)
        labels = {
            "payment_days": "earlier payment",
            "contract_months": "a longer commitment",
            "volume_per_year": "a larger annual volume",
            "lead_time_days": "a longer lead time",
            "ppm_commitment": "an adjusted quality commitment",
            "chargeback_eur_per_complaint": "a lower chargeback amount",
        }
        trade_descriptions = []
        for package in packages[1:]:
            for field, label in labels.items():
                if package.get(field) != packages[0].get(field) and label not in trade_descriptions:
                    trade_descriptions.append(label)
        draft = writer.draft(case, latest(threads[sid]), evidence_line, trade_descriptions,
                             history=threads[sid][-6:])
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
