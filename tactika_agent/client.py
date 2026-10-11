"""Small standalone HTTP client for the Tacto Arena v1 API."""

from __future__ import annotations

import os
import threading
import time
import uuid

import httpx


class ArenaError(RuntimeError):
    def __init__(self, status, detail):
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class ArenaClient:
    def __init__(self, base_url=None, api_key=None, retries=3, timeout=180.0, rate=9):
        host = (base_url or os.getenv("ARENA_URL", "")).strip().rstrip("/")
        if not host:
            raise ValueError("Arena URL is missing. Set ARENA_URL in .env before using Arena commands.")
        token = (api_key or os.getenv("ARENA_API_KEY") or "").strip()
        headers = {"X-API-Key": token} if token else {}
        self.http = httpx.Client(base_url=f"{host}/v1", headers=headers, timeout=timeout)
        self.retries = retries
        self.rate = rate
        self._pace_lock = threading.Lock()
        self._next_request = 0.0
        self.practice_remaining = None

    def close(self):
        self.http.close()

    def _pace(self):
        with self._pace_lock:
            now = time.monotonic()
            pause = self._next_request - now
            self._next_request = max(now, self._next_request) + 1 / self.rate
        if pause > 0:
            time.sleep(pause)

    def _request(self, method, path, body=None, params=None, raw=False, move_id=None):
        move_id = (move_id or uuid.uuid4().hex) if method == "POST" else None
        headers = {"X-Move-Id": move_id} if move_id else {}
        for attempt in range(self.retries + 1):
            self._pace()
            try:
                response = self.http.request(method, path, json=body, params=params, headers=headers)
            except httpx.TransportError:
                if attempt == self.retries:
                    raise
                time.sleep(0.5 * (2**attempt))
                continue

            remaining = response.headers.get("X-Practice-Remaining", "")
            if remaining.isdigit():
                self.practice_remaining = int(remaining)
            retry_after = response.headers.get("Retry-After", "")
            retryable = response.status_code in {429, 500, 502, 503, 504}
            short_wait = retry_after.isdigit() and int(retry_after) <= 5
            if retryable and attempt < self.retries and (response.status_code != 429 or short_wait):
                time.sleep(float(retry_after) if short_wait else 0.5 * (2**attempt))
                continue
            if response.status_code >= 400:
                try:
                    payload = response.json()
                    detail = payload.get("detail", payload)
                except (ValueError, AttributeError):
                    detail = response.text
                if response.status_code == 401:
                    detail = "Arena rejected the API key. Check ARENA_API_KEY in the loaded .env file."
                raise ArenaError(response.status_code, detail)
            return response.content if raw else response.json()
        raise RuntimeError("request retry loop ended without a response")

    def me(self):
        return self._request("GET", "/me")

    def cases(self, set_name="practice"):
        return self._request("GET", "/cases", params={"set": set_name})

    def open_session(self, case_id, move_id=None):
        return self._request("POST", "/sessions", {"case_id": case_id}, move_id=move_id)

    def session(self, session_id):
        return self._request("GET", f"/sessions/{session_id}")

    def thread(self, session_id, supplier_id):
        return self._request("GET", f"/sessions/{session_id}/threads/{supplier_id}")

    def send_message(self, session_id, supplier_id, message, move_id=None):
        route = f"/sessions/{session_id}/threads/{supplier_id}/messages"
        return self._request("POST", route, {"message": message}, move_id=move_id)

    def attachment(self, session_id, attachment_id):
        return self._request("GET", f"/sessions/{session_id}/attachments/{attachment_id}", raw=True)
