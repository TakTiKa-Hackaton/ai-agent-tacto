"""Draft negotiation prose without allowing a model to choose a price or make numeric claims."""

from __future__ import annotations

import os
import re

import httpx

UNSAFE = re.compile(r"\d|(?:€|\bEUR\b|\bUSD\b|\b(?:BATNA|walk[- ]away|budget|target price)\b)", re.I)


class EmailWriter:
    def __init__(self):
        self.model = os.getenv("LLM_MODEL", "").strip()

    @staticmethod
    def fallback(case, reply, has_packages):
        body = (reply or {}).get("body", "")
        lower = body.lower()
        if any(word in lower for word in ("capacity", "volume", "forecast", "allocation")):
            question = "What volume or forecast commitment would make a more competitive proposal workable?"
        elif any(word in lower for word in ("payment", "cash flow", "liquidity")):
            question = "Which combination of payment timing and commercial terms would give you room to improve the offer?"
        elif any(word in lower for word in ("quality", "ppm", "defect")):
            question = "Could you outline the quality actions and service levels you can commit to in the revised proposal?"
        elif any(word in lower for word in ("material", "energy", "wage", "index", "cost")):
            question = "Could you share the main cost drivers and the portion that applies to this part?"
        else:
            question = "Please review the attached commercial options and let us know which is workable on your side."
        if has_packages:
            opening = (
                "Thank you for your response. We have reviewed the proposal and prepared "
                "alternative commercial terms for your consideration. "
            )
        else:
            opening = "Thank you for your response. We need one point clarified before confirming terms. "
        return "Dear Supplier,\n\n" + opening + question + "\n\nKind regards,\nTaktika Procurement"

    def draft(self, case, reply, evidence, has_packages):
        if not self.model:
            return self.fallback(case, reply, offers)
        api_key = os.getenv("LLM_API_KEY", "")
        base = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        prompt = (
            "Write a concise procurement email reply (under 90 words) for Hollerau Pumpenwerke. "
            "Acknowledge the supplier's latest message, then ask one useful question or explain a "
            "reciprocal trade. Offer terms have already been selected by code. Do not mention any "
            "number, price, percentage, date, citation, budget, target, or internal calculation. "
            "Return only the email text.\n\n"
            f"Case: {case.get('title', '')}\nSupplier reply: {(reply or {}).get('body', '')[:1600]}\n"
            f"Verified evidence context: {evidence}\nCommercial options attached: {bool(has_packages)}"
        )
        try:
            response = httpx.post(
                f"{base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "You draft prose only. Never decide the terms."},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": 0.3,
                },
                timeout=25,
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"].strip()
            if not text or UNSAFE.search(text) or len(text.split()) > 120:
                return self.fallback(case, reply, offers)
            return text
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            return self.fallback(case, reply, offers)
