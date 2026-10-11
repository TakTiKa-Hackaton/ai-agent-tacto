"""Offline metamorphic cases for category, market, and supplier-quote variation."""

from __future__ import annotations

import math
import unittest
from datetime import date

from tactika_agent.strategy import OfferPlanner


class Researcher:
    @staticmethod
    def weighted_market_change(research):
        return research["market_change"]


class GeneralizationRegressionTests(unittest.TestCase):
    def test_planner_handles_sixty_unseen_category_quote_and_market_combinations(self):
        goals = ("stabilise", "secure_supply", "quality_first", "leverage", "routine")
        quote_profiles = {
            "cash_constrained": 82,
            "capacity_constrained": 88,
            "quality_sensitive": 94,
            "relationship_sensitive": 101,
        }
        market_moves = (-10, 0, 10)
        planner = OfferPlanner(Researcher())
        count = 0

        for goal in goals:
            for profile, quote_price in quote_profiles.items():
                for move in market_moves:
                    case = {
                        "case_id": f"unseen:{goal}:{profile}:{move}",
                        "parts": ["part:unseen"],
                        "negotiable": ["unit_price"],
                        "term_options": {},
                        "buyer": {
                            "category_goal": goal,
                            "current_unit_price": 100,
                            "fallback_unit_price": 120,
                            "need_per_year": 10,
                            "horizon_years": 1,
                            "reference_payment_days": 30,
                            "need_lead_time_days": 30,
                            "freight_eur_per_unit": {"EXW": 0, "FCA": 1, "DAP": 2},
                            "must_haves": [],
                        },
                    }
                    research = {
                        "market_change": move,
                        "purchase_history": [],
                        "should_cost": [],
                        "quotes": [{
                            "part_id": "part:unseen",
                            "supplier_id": f"competitor:{profile}",
                            "price": quote_price,
                            "awarded": True,
                            "payment_days": 30,
                            "lead_time_days": 30,
                            "incoterm": "EXW",
                            "tooling_eur": 0,
                            "date": date.today().isoformat(),
                            "validity_days": 30,
                        }],
                    }
                    target = planner.target_price(case, "supplier:unseen", {}, research)
                    self.assertTrue(math.isfinite(target))
                    self.assertGreater(target, 0)
                    self.assertLessEqual(target, quote_price)
                    count += 1

        self.assertEqual(count, 60)

    def test_case_and_supplier_identifiers_do_not_change_price_policy(self):
        planner = OfferPlanner(Researcher())
        case = {
            "case_id": "case:original",
            "parts": ["part:one"],
            "negotiable": ["unit_price"],
            "term_options": {},
            "buyer": {
                "category_goal": "routine", "current_unit_price": 100,
                "fallback_unit_price": 120, "freight_eur_per_unit": {"EXW": 0},
                "must_haves": [],
            },
        }
        research = {"market_change": 0, "quotes": [], "purchase_history": [],
                    "should_cost": []}
        renamed = {**case, "case_id": "case:unseen", "parts": ["part:renamed"]}

        self.assertEqual(planner.target_price(case, "supplier:one", {}, research),
                         planner.target_price(renamed, "supplier:renamed", {}, research))


if __name__ == "__main__":
    unittest.main()
