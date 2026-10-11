"""Relationship-safe email drafting regressions; no network requests are made."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tactika_agent.email_writer import EmailWriter


class Response:
    def raise_for_status(self):
        return None

    def __init__(self, text):
        self.text = text

    def json(self):
        return {"choices": [{"message": {"content": self.text}}]}


class EmailWriterRegressionTests(unittest.TestCase):
    def test_unsubstantiated_supplier_threat_uses_safe_fallback(self):
        with patch.dict(os.environ, {"LLM_API_KEY": "test", "LLM_MODEL": "gpt-6-luna"}), \
             patch("tactika_agent.email_writer.httpx.post",
                   return_value=Response("Our other supplier will take all your future orders.")):
            writer = EmailWriter()
            result = writer.draft({}, {"body": "Thanks for the update."}, "", [])

        self.assertEqual(result, writer.fallback({}, {"body": "Thanks for the update."}, []))

    def test_relationship_prompt_receives_recent_exchange_and_forbids_invented_claims(self):
        captured = {}

        def post(_url, **kwargs):
            captured["prompt"] = kwargs["json"]["messages"][1]["content"]
            return Response("Thank you for explaining the capacity concern. Which delivery schedule can you support?")

        history = [
            {"from": "buyer", "body": "We can maintain the agreed payment schedule."},
            {"from": "supplier", "body": "We cannot increase output this month."},
        ]
        with patch.dict(os.environ, {"LLM_API_KEY": "test", "LLM_MODEL": "gpt-6-luna"}), \
             patch("tactika_agent.email_writer.httpx.post", side_effect=post):
            result = EmailWriter().draft({}, history[-1], "", [], history=history)

        self.assertIn("We can maintain the agreed payment schedule.", captured["prompt"])
        self.assertIn("Do not invent", captured["prompt"])
        self.assertNotIn("all your future orders", result)


if __name__ == "__main__":
    unittest.main()
