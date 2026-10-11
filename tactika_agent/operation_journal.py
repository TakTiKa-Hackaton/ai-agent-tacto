"""Durable idempotency journal for externally retried MCP mutations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path


class OperationJournal:
    def __init__(self, state_dir=None, arena_url="", api_key=""):
        root = Path(state_dir or os.getenv("TAKTIKA_STATE_DIR", "~/.taktika-buyer-agent")).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        self.path = root / "mcp-operations.sqlite3"
        self.namespace = hashlib.sha256(f"{arena_url}\0{api_key}".encode()).hexdigest()
        with self._connect() as database:
            database.execute("""
                CREATE TABLE IF NOT EXISTS operations (
                    namespace TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    move_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'complete')),
                    response_json TEXT,
                    context_json TEXT,
                    PRIMARY KEY(namespace, scope_key, operation_id)
                )
            """)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self):
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        return database

    @staticmethod
    def _json(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

    def begin(self, scope, operation_id, payload, proposed_move_id):
        scope_key = self._json(scope)
        payload_hash = hashlib.sha256(self._json(payload).encode()).hexdigest()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            row = database.execute(
                "SELECT * FROM operations WHERE namespace=? AND scope_key=? AND operation_id=?",
                (self.namespace, scope_key, operation_id),
            ).fetchone()
            if row:
                if row["payload_hash"] != payload_hash:
                    raise ValueError("operation_id was already used with a different request")
                return {
                    "move_id": row["move_id"], "status": row["status"],
                    "response": json.loads(row["response_json"]) if row["response_json"] else None,
                    "context": json.loads(row["context_json"]) if row["context_json"] else None,
                }
            database.execute(
                "INSERT INTO operations VALUES (?, ?, ?, ?, ?, 'pending', NULL, NULL)",
                (self.namespace, scope_key, operation_id, payload_hash, proposed_move_id),
            )
            return {"move_id": proposed_move_id, "status": "pending", "response": None,
                    "context": None}

    def lookup(self, scope, operation_id, payload):
        scope_key = self._json(scope)
        payload_hash = hashlib.sha256(self._json(payload).encode()).hexdigest()
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM operations WHERE namespace=? AND scope_key=? AND operation_id=?",
                (self.namespace, scope_key, operation_id),
            ).fetchone()
        if not row:
            return None
        if row["payload_hash"] != payload_hash:
            raise ValueError("operation_id was already used with a different request")
        return {
            "move_id": row["move_id"], "status": row["status"],
            "response": json.loads(row["response_json"]) if row["response_json"] else None,
            "context": json.loads(row["context_json"]) if row["context_json"] else None,
        }

    def complete(self, scope, operation_id, response, context=None):
        with self._connect() as database:
            database.execute(
                "UPDATE operations SET status='complete', response_json=?, context_json=? "
                "WHERE namespace=? AND scope_key=? AND operation_id=?",
                (self._json(response), self._json(context) if context is not None else None,
                 self.namespace, self._json(scope), operation_id),
            )

    def discard(self, scope, operation_id):
        with self._connect() as database:
            database.execute(
                "DELETE FROM operations WHERE namespace=? AND scope_key=? AND operation_id=? "
                "AND status='pending'",
                (self.namespace, self._json(scope), operation_id),
            )
