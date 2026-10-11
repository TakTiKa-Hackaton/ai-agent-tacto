"""Regression tests for the model-assisted negotiation council."""

from __future__ import annotations

import json as jsonlib
import unittest

from tactika_agent.council import (
    FIELD_OWNERS, ModelUsageBudget, SpecialistCouncil, expected_cost, validate_risk,
)
from tactika_agent.strategy import OfferPlanner


class CouncilPolicyTests(unittest.TestCase):
    def test_every_arena_term_has_exactly_one_category_owner(self):
        arena_terms = {
            "unit_price", "payment_days", "lead_time_days", "volume_per_year",
            "contract_months", "effective_date", "index_clause", "incoterm",
            "tooling_eur", "ppm_commitment", "chargeback_eur_per_complaint",
        }
        owned = [term for terms in FIELD_OWNERS.values() for term in terms]

        self.assertEqual(set(owned), arena_terms)
        self.assertEqual(len(owned), len(set(owned)))

    def test_risk_estimate_requires_a_complete_probability_distribution(self):
        estimate = validate_risk({
            "accept": 0.25, "counter": 0.65, "supplier_walkaway": 0.10,
            "confidence": "low",
        })

        self.assertEqual(estimate["accept"], 0.25)
        self.assertEqual(estimate["supplier_walkaway"], 0.10)
        with self.assertRaises(ValueError):
            validate_risk({"accept": 0.7, "counter": 0.4, "supplier_walkaway": 0})

    def test_expected_cost_prices_acceptance_counter_and_walkaway_separately(self):
        estimate = {"accept": 0.25, "counter": 0.65, "supplier_walkaway": 0.10}

        self.assertEqual(expected_cost(estimate, offer_cost=90, recovery_cost=100, fallback_cost=200), 107.5)

    def test_usage_budget_refuses_concurrent_requests_that_would_cross_the_limit(self):
        budget = ModelUsageBudget(0.01)

        reservation = budget.reserve("gpt-4o", prompt_bytes=2500, max_output_tokens=100)

        self.assertIsNotNone(reservation)
        self.assertIsNone(budget.reserve("gpt-4o", prompt_bytes=2500, max_output_tokens=100))
        budget.settle(reservation, {"prompt_tokens": 100, "completion_tokens": 10}, "gpt-4o")
        self.assertAlmostEqual(budget.spent_usd, 0.00035)

    def test_usage_budget_cannot_be_configured_above_five_dollars(self):
        self.assertEqual(ModelUsageBudget(100).limit_usd, 5.0)

    def test_malformed_empty_api_response_fails_back_safely(self):
        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"choices": []}

        council = SpecialistCouncil(api_key="test-key", enabled=True, budget_usd=0.1,
                                    post=lambda *_args, **_kwargs: Response())

        self.assertIsNone(council._request("money", "review", {}))

    def test_candidate_generator_rejects_terms_outside_arena_options(self):
        class Researcher:
            @staticmethod
            def weighted_market_change(_research):
                return 0

        case = {
            "negotiable": ["unit_price", "payment_days"],
            "term_options": {"payment_days": [30, 60]},
            "fixed_terms": {"lead_time_days": 1, "incoterm": "DAP"},
            "buyer": {
                "need_per_year": 1, "horizon_years": 1, "interest_rate": 0,
                "reference_payment_days": 30, "need_lead_time_days": 1,
                "fallback_unit_price": 100, "freight_eur_per_unit": {"DAP": 0},
                "alternative": {"terms": {"unit_price": 100}}, "must_haves": [],
            },
        }
        threads = {"sup-1": [{"from": "supplier", "status": "open", "term_sheets": [
            {"term_sheet_id": "offer-1", "terms": {"unit_price": 10, "payment_days": 30}},
        ]}]}
        planner = OfferPlanner(Researcher())
        research = {key: [] for key in ("quotes", "purchase_history", "should_cost", "market_moves", "refs")}
        research["scorecards"] = {}
        packages = planner.offer_packages(case, threads, "sup-1", research)

        result = planner.council_packages(
            case, threads, "sup-1", research, [{"terms": item} for item in packages],
            {"payment": {"payment_days": 45}},
        )
        self.assertEqual(result, packages)
        self.assertTrue(planner.is_safe_specialist_candidate(
            case, threads, "sup-1", result[0], research,
            [{"terms": item} for item in packages],
        ))
        self.assertFalse(planner.is_safe_specialist_candidate(
            case, threads, "sup-1", {**result[0], "unit_price": 1000}, research,
            [{"terms": item} for item in packages],
        ))

        invalid_money = planner.council_packages(
            case, threads, "sup-1", research, [{"terms": item} for item in packages],
            {"money": {"unit_price": "free"}},
        )
        self.assertEqual(invalid_money, packages)

    def test_boss_can_send_only_a_validated_lower_expected_cost_package(self):
        class Planner:
            @staticmethod
            def decide(*_args):
                return {"supplier_id": "sup-1", "message": {"term_sheets": [
                    {"terms": {"unit_price": 95}},
                ]}}

            @staticmethod
            def council_packages(*args):
                return [item["terms"] for item in args[4]] + [{"unit_price": 90}]

            @staticmethod
            def _state(*_args):
                return {"cost": 95, "valid": True, "standard": {}}

            @staticmethod
            def batna(*_args):
                return 200, "alternative"

            @staticmethod
            def cost(_case, terms, *_args):
                return terms["unit_price"]

            @staticmethod
            def is_safe_specialist_candidate(*_args):
                return True

        def post(_url, headers, json, timeout):
            role = json["messages"][0]["content"]
            if "rejection_risk" in role:
                context = jsonlib.loads(json["messages"][1]["content"])
                response = {"predictions": {
                    candidate: {"accept": 0.99, "counter": 0.005,
                                "supplier_walkaway": 0.005, "confidence": "high"}
                    for candidate in context["candidates"]
                }}
            elif "boss" in role:
                context = jsonlib.loads(json["messages"][1]["content"])
                candidate = context["offer_candidate_ids"][0]
                response = {"action": "offer_one_to_three_candidates",
                            "candidate_ids": [candidate], "reason": "Lower expected cost."}
            else:
                response = {"relevant": "money" in role, "confidence": 0.9,
                            "findings": "Reviewed available commercial evidence.",
                            "refs": [], "terms": {"unit_price": 90} if "money" in role else {}}

            class Response:
                def raise_for_status(self):
                    return None

                @staticmethod
                def json():
                    return {"choices": [{"message": {"content": jsonlib.dumps(response)}}],
                            "usage": {"prompt_tokens": 20, "completion_tokens": 20}}

            return Response()

        case = {"case_id": "case:test", "title": "Test", "briefing": "Renewal",
                "negotiable": ["unit_price"], "term_options": {}, "fixed_terms": {},
                "buyer": {"category_goal": "routine"}, "refs": []}
        threads = {"sup-1": [{"from": "supplier", "status": "open", "term_sheets": [
            {"term_sheet_id": "offer-1", "terms": {"unit_price": 95}},
        ]}]}
        council = SpecialistCouncil(api_key="test-key", model="gpt-6-luna", enabled=True,
                                    budget_usd=0.10, post=post)

        decision = council.decide(case, threads, {"refs": []}, Planner())

        self.assertEqual(decision["message"]["term_sheets"], [{"terms": {"unit_price": 90}}])
        self.assertEqual(council.last_audit["status"], "council_offer")

    def test_malformed_risk_response_falls_back_to_the_planner(self):
        class Planner:
            baseline = {"supplier_id": "sup-1", "message": {"term_sheets": [
                {"terms": {"unit_price": 95}},
            ]}}

            @classmethod
            def decide(cls, *_args):
                return cls.baseline

            @staticmethod
            def council_packages(*args):
                return [item["terms"] for item in args[4]]

            @staticmethod
            def _state(*_args):
                return {"cost": 95, "valid": True, "standard": {}}

            @staticmethod
            def batna(*_args):
                return 200, "alternative"

            @staticmethod
            def cost(_case, terms, *_args):
                return terms["unit_price"]

        def post(_url, headers, json, timeout):
            role = json["messages"][0]["content"]
            if "rejection_risk" in role:
                context = jsonlib.loads(json["messages"][1]["content"])
                response = {"predictions": {
                    candidate: {"accept": 0.4, "counter": 0.4,
                                "supplier_walkaway": 0.1, "confidence": "high"}
                    for candidate in context["candidates"]
                }}
            else:
                response = {"relevant": True, "confidence": 0.9, "findings": "Reviewed.",
                            "refs": [], "terms": {}}

            class Response:
                def raise_for_status(self):
                    return None

                @staticmethod
                def json():
                    return {"choices": [{"message": {"content": jsonlib.dumps(response)}}],
                            "usage": {"prompt_tokens": 20, "completion_tokens": 20}}

            return Response()

        case = {"case_id": "case:test", "title": "Test", "briefing": "Renewal",
                "negotiable": ["unit_price"], "term_options": {}, "fixed_terms": {},
                "buyer": {"category_goal": "routine"}, "refs": []}
        threads = {"sup-1": [{"from": "supplier", "status": "open", "term_sheets": [
            {"term_sheet_id": "offer-1", "terms": {"unit_price": 95}},
        ]}]}
        council = SpecialistCouncil(api_key="test-key", model="gpt-6-luna", enabled=True,
                                    budget_usd=0.10, post=post)

        decision = council.decide(case, threads, {"refs": []}, Planner())

        self.assertIs(decision, Planner.baseline)
        self.assertEqual(council.last_audit["status"], "risk_fallback")

    def test_boss_can_walk_away_when_no_safe_offer_beats_a_real_alternative(self):
        class Planner:
            baseline = {"supplier_id": "sup-1", "message": {"term_sheets": [
                {"terms": {"unit_price": 95}},
            ]}}

            @classmethod
            def decide(cls, *_args):
                return cls.baseline

            @staticmethod
            def council_packages(*args):
                return [item["terms"] for item in args[4]]

            @staticmethod
            def _state(*_args):
                return {"cost": 100, "valid": False, "standard": {}}

            @staticmethod
            def batna(*_args):
                return 100, "documented alternative"

            @staticmethod
            def cost(_case, terms, *_args):
                return terms["unit_price"]

        def post(_url, headers, json, timeout):
            role = json["messages"][0]["content"]
            if "rejection_risk" in role:
                context = jsonlib.loads(json["messages"][1]["content"])
                response = {"predictions": {
                    candidate: {"accept": 0, "counter": 0, "supplier_walkaway": 1,
                                "confidence": "high"}
                    for candidate in context["candidates"]
                }}
            elif "boss" in role:
                response = {"action": "walk_away", "candidate_ids": [],
                            "reason": "Every proposed package loses the supplier."}
            else:
                response = {"relevant": True, "confidence": 0.9, "findings": "Reviewed.",
                            "refs": [], "terms": {}}

            class Response:
                def raise_for_status(self):
                    return None

                @staticmethod
                def json():
                    return {"choices": [{"message": {"content": jsonlib.dumps(response)}}],
                            "usage": {"prompt_tokens": 20, "completion_tokens": 20}}

            return Response()

        case = {"case_id": "case:test", "title": "Test", "briefing": "Renewal",
                "negotiable": ["unit_price"], "term_options": {}, "fixed_terms": {},
                "buyer": {"category_goal": "routine"}, "refs": []}
        threads = {"sup-1": [{"from": "supplier", "status": "open", "term_sheets": [
            {"term_sheet_id": "offer-1", "terms": {"unit_price": 95}},
        ]}]}
        council = SpecialistCouncil(api_key="test-key", model="gpt-6-luna", enabled=True,
                                    budget_usd=0.10, post=post)

        decision = council.decide(case, threads, {"refs": []}, Planner())

        self.assertEqual(decision["message"]["action"]["type"], "walk_away")
        self.assertEqual(council.last_audit["status"], "council_walkaway")


if __name__ == "__main__":
    unittest.main()
