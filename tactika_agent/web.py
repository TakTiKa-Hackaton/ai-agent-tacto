"""Local web dashboard that runs Taktika autonomously and streams deal activity."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

from .agent import TaktikaAgent
from .client import ArenaClient
from .play import RemoteTable, hosted, play

log = logging.getLogger("taktika.web")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = PROJECT_ROOT / "taktichek.html"
DATA_ROOT = PROJECT_ROOT / "data" / "data"
WORKERS = ThreadPoolExecutor(max_workers=3, thread_name_prefix="taktichek-deal")


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


class DealStore:
    """Small local SQLite event store; negotiation records stay outside the repository."""

    def __init__(self, path=None):
        self.path = Path(path or os.getenv("TAKTICHEK_DB", "~/.taktichek/dashboard.sqlite3")).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS deals (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL, set_name TEXT NOT NULL,
                    status TEXT NOT NULL, title TEXT NOT NULL, case_json TEXT NOT NULL,
                    session_id TEXT, result_json TEXT, error TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deal_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, deal_id TEXT NOT NULL,
                    event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    FOREIGN KEY(deal_id) REFERENCES deals(id)
                );
                CREATE INDEX IF NOT EXISTS deal_events_by_deal ON deal_events(deal_id,id);
            """)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        self.suppliers = self._catalog("suppliers.csv")
        self.parts = self._catalog("parts.csv")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _catalog(filename):
        import csv

        path = DATA_ROOT / filename
        if not path.exists():
            return {}
        with path.open(newline="", encoding="utf-8-sig") as source:
            return {row.get("id", ""): row for row in csv.DictReader(source)}

    def supplier_name(self, supplier_id):
        return self.suppliers.get(str(supplier_id), {}).get("name") or str(supplier_id)

    def part_name(self, part_id):
        return self.parts.get(str(part_id), {}).get("description") or str(part_id)

    def case_summary(self, case):
        buyer = case.get("buyer", {})
        return {
            "case_id": case.get("case_id", ""),
            "title": case.get("title") or case.get("briefing") or case.get("case_id", "Deal"),
            "type": case.get("type", "procurement"),
            "parts": [self.part_name(part) for part in case.get("parts", [])],
            "suppliers": [
                {"id": supplier, "name": self.supplier_name(supplier)}
                for supplier in case.get("suppliers", [])
            ],
            "briefing": case.get("briefing", ""),
            "buyer": {
                key: buyer[key]
                for key in ("current_unit_price", "reference_payment_days")
                if buyer.get(key) is not None
            },
        }

    def create(self, case, set_name):
        deal_id = uuid.uuid4().hex
        stamp = now()
        title = case.get("title") or case.get("case_id", "Autonomous deal")
        with self.connect() as db:
            db.execute(
                "INSERT INTO deals(id,case_id,set_name,status,title,case_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (deal_id, case["case_id"], set_name, "queued", title, encode(case), stamp, stamp),
            )
        return deal_id

    def set_session(self, deal_id, session_id, case):
        with self.connect() as db:
            db.execute(
                "UPDATE deals SET session_id=?,case_json=?,status='running',updated_at=? WHERE id=?",
                (session_id, encode(case), now(), deal_id),
            )

    def set_status(self, deal_id, status, result=None, error=None):
        with self.connect() as db:
            db.execute(
                "UPDATE deals SET status=?,result_json=?,error=?,updated_at=? WHERE id=?",
                (status, encode(result) if result is not None else None, error, now(), deal_id),
            )

    def emit(self, deal_id, event_type, payload):
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO deal_events(deal_id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
                (deal_id, event_type, encode(payload), now()),
            )
            db.execute("UPDATE deals SET updated_at=? WHERE id=?", (now(), deal_id))
            return cursor.lastrowid

    def get_events(self, deal_id, after=0):
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,event_type,payload_json,created_at FROM deal_events "
                "WHERE deal_id=? AND id>? ORDER BY id", (deal_id, after),
            ).fetchall()
        return [
            {"id": row["id"], "type": row["event_type"], "payload": json.loads(row["payload_json"]),
             "created_at": row["created_at"]}
            for row in rows
        ]

    def get_row(self, deal_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
        return row

    def summary(self, row):
        case = json.loads(row["case_json"])
        events = self.get_events(row["id"])
        stage = next((e["payload"].get("summary") for e in reversed(events)
                      if e["type"] == "agent_progress"), None)
        return {
            "id": row["id"], "case_id": row["case_id"], "set": row["set_name"],
            "status": row["status"], "title": row["title"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "session_id": row["session_id"], "case": self.case_summary(case),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": row["error"], "stage": stage,
            "last_event_id": events[-1]["id"] if events else 0,
        }

    def list_deals(self, set_name=None):
        with self.connect() as db:
            if set_name:
                rows = db.execute("SELECT * FROM deals WHERE set_name=? ORDER BY created_at DESC", (set_name,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM deals ORDER BY created_at DESC").fetchall()
        return [self.summary(row) for row in rows]

    def detail(self, deal_id):
        row = self.get_row(deal_id)
        if row is None:
            return None
        result = self.summary(row)
        result["events"] = self.get_events(deal_id)
        return result


class ObservedTable(RemoteTable):
    def __init__(self, client, store, deal_id):
        super().__init__(client)
        self.store, self.deal_id = store, deal_id

    def _emit_message(self, event_type, supplier_id, message):
        self.store.emit(self.deal_id, event_type, {
            "supplier_id": supplier_id,
            "supplier_name": self.store.supplier_name(supplier_id),
            "message": message,
        })

    def open(self, case_id):
        case, threads = super().open(case_id)
        self.store.set_session(self.deal_id, self.session_id, case)
        self.store.emit(self.deal_id, "session_started", {
            "session_id": self.session_id,
            "case": self.store.case_summary(case),
        })
        for supplier_id, messages in threads.items():
            supplier_message = next((message for message in reversed(messages)
                                     if message.get("from") != "buyer"), None)
            if supplier_message:
                self._emit_message("supplier_email", supplier_id, supplier_message)
        return case, threads

    def send(self, supplier, message):
        reply = super().send(supplier, message)
        if message.get("action"):
            self.store.emit(self.deal_id, "deal_action", {
                "supplier_id": supplier,
                "action": message["action"],
            })
        else:
            self._emit_message("buyer_email", supplier, {**message, "from": "buyer"})
        if "result" in reply:
            self.store.emit(self.deal_id, "deal_result", {"result": reply["result"]})
        elif not message.get("action"):
            self._emit_message("supplier_email", supplier, reply)
        return reply

    def close(self, supplier, action):
        self.store.emit(self.deal_id, "agent_progress", {
            "stage": "closing", "summary": "Applying the autonomous close policy to the supplier’s latest offer.",
        })
        reply = super().close(supplier, action)
        if "result" in reply:
            self.store.emit(self.deal_id, "deal_result", {"result": reply["result"]})
        return reply


class ObservedAgent:
    """Emit safe progress summaries around the private negotiation decision."""

    ROLE_LABELS = {
        "money": ("Commercial Specialist", "Evaluates price and tooling against total cost and alternatives."),
        "payment": ("Payment Specialist", "Checks payment timing and supported cash-flow trade-offs."),
        "delivery": ("Delivery Specialist", "Checks lead time, freight, Incoterms, and delivery requirements."),
        "quality": ("Quality Specialist", "Checks scorecards, quality commitments, and defect costs."),
        "contract": ("Contract Specialist", "Checks permitted volume, contract duration, and effective date."),
        "market_index": ("Market Index Specialist", "Checks disclosed cost drivers and index-clause options."),
        "relationship": ("Supplier Relationship Specialist", "Checks evidence about supplier health and relationship risk."),
        "evidence_alternatives": ("Evidence & Alternatives Specialist", "Checks evidence, comparability, and available alternatives."),
    }

    def __init__(self, agent, store, deal_id):
        self.agent, self.store, self.deal_id = agent, store, deal_id

    def decide(self, case, threads):
        council = self.agent.council
        applicable = council._applicable_roles(case) if council.enabled else []
        activities = [
            {"name": "Taktichek Coordinator", "purpose": "Orchestrates the autonomous supplier negotiation.",
             "status": "Working", "activity": "Coordinating the case decision."},
            {"name": "Procurement Researcher", "purpose": "Reviews supplier evidence, scorecards, history, and market data.",
             "status": "Working", "activity": "Collecting case and supplier evidence."},
            {"name": "Offer Planner", "purpose": "Builds offers and checks company limits and alternatives.",
             "status": "Working", "activity": "Preparing a bounded offer decision."},
        ] + [
            {
                "name": label,
                "purpose": purpose,
                "status": "Working" if role in applicable else "Not enabled" if not council.enabled else "Not needed",
                "activity": purpose if role in applicable else "This specialist is not active for the current case.",
            }
            for role, (label, purpose) in self.ROLE_LABELS.items()
        ]
        self.store.emit(self.deal_id, "agent_progress", {
            "stage": "research", "summary": "Reviewing supplier messages, documents, history, and market evidence.",
            "agents": activities,
        })
        decision = self.agent.decide(case, threads)
        audit = self.agent.last_council or {}
        specialists = audit.get("specialists", {})
        errors = audit.get("errors", {})
        for role, activity in zip(self.ROLE_LABELS, activities[3:]):
            if activity["status"] in {"Not enabled", "Not needed"}:
                continue
            if role in specialists:
                activity.update(status="Complete", activity="Completed a bounded review for its commercial area.")
            elif role in errors:
                activity.update(status="Fallback", activity="No usable specialist advice; deterministic planner safeguards remain active.")
            else:
                activity.update(status="Fallback", activity="No specialist result was used; deterministic planner safeguards remain active.")
        for activity in activities[:3]:
            activity.update(status="Complete", activity="Completed this step for the current negotiation turn.")
        activities.append({
            "name": "Email Composer",
            "purpose": "Drafts a clear, respectful response based on the negotiation decision.",
            "status": "Complete" if decision.get("message", {}).get("body") else "Not needed",
            "activity": "Prepared the supplier response." if decision.get("message", {}).get("body")
            else "No email was needed for this close decision.",
        })
        self.store.emit(self.deal_id, "agent_progress", {
            "stage": "negotiation", "summary": "Offer checked against must-have terms and the best alternative; preparing the supplier reply.",
            "agents": activities,
        })
        return decision

    def close_decision(self, case, threads):
        self.store.emit(self.deal_id, "agent_progress", {
            "stage": "closing", "summary": "Evaluating the latest offer against the target, company limits, and alternatives.",
        })
        return self.agent.close_decision(case, threads)


def run_deal(store, deal_id, case_id):
    client = None
    try:
        store.set_status(deal_id, "running")
        store.emit(deal_id, "agent_progress", {
            "stage": "starting", "summary": "Opening the supplier conversation and preparing the first autonomous turn.",
        })
        client = ArenaClient()
        result = play(ObservedTable(client, store, deal_id), case_id,
                      ObservedAgent(TaktikaAgent(), store, deal_id))
        store.set_status(deal_id, "completed", result=result)
        store.emit(deal_id, "deal_completed", {"result": result})
    except Exception as error:
        log.exception("Autonomous deal %s failed", deal_id)
        store.set_status(deal_id, "failed", error=f"{type(error).__name__}: {error}")
        store.emit(deal_id, "deal_failed", {"message": f"{type(error).__name__}: {error}"})
    finally:
        if client:
            client.close()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store):
        super().__init__(address, DashboardHandler)
        self.store = store


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "TaktichekDashboard/1.0"

    @property
    def store(self):
        return self.server.store

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _json(self, status, payload):
        body = encode(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16384:
            raise ValueError("request body is too large")
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        request = urlparse(self.path)
        path = request.path
        if path == "/" or path == "/taktichek.html":
            if not PAGE_PATH.exists():
                return self._json(404, {"error": "taktichek.html was not found at the project root"})
            content = PAGE_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)
            return
        if path == "/api/health":
            ready = bool(os.getenv("ARENA_URL") and os.getenv("ARENA_API_KEY"))
            return self._json(200, {"ok": True, "arena_configured": ready})
        if path == "/api/cases":
            return self._cases(parse_qs(request.query))
        if path == "/api/deals":
            set_name = parse_qs(request.query).get("set", [None])[0]
            return self._json(200, {"deals": self.store.list_deals(set_name)})
        if path.startswith("/api/deals/") and path.endswith("/events"):
            deal_id = path.split("/")[3]
            return self._events(deal_id, parse_qs(request.query))
        if path.startswith("/api/deals/"):
            deal_id = path.split("/")[3]
            detail = self.store.detail(deal_id)
            if detail is None:
                return self._json(404, {"error": "deal not found"})
            return self._json(200, detail)
        return self._json(404, {"error": "not found"})

    def _cases(self, query):
        set_name = query.get("set", ["sandbox"])[0]
        if set_name not in {"practice", "sandbox"}:
            return self._json(400, {"error": "set must be practice or sandbox"})
        client = None
        try:
            client = ArenaClient()
            cases = client.cases(set_name)
            if set_name == "practice":
                cases = hosted(cases)
            visible = []
            for case in cases:
                item = self.store.case_summary(case)
                item["set"] = set_name
                visible.append(item)
            return self._json(200, {"cases": visible})
        except Exception as error:
            return self._json(503, {"error": f"Could not load Arena cases: {error}"})
        finally:
            if client:
                client.close()

    def _events(self, deal_id, query):
        if self.store.get_row(deal_id) is None:
            return self._json(404, {"error": "deal not found"})
        try:
            after = max(0, int(query.get("after", ["0"])[0]))
        except ValueError:
            after = 0
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            while True:
                events = self.store.get_events(deal_id, after)
                for event in events:
                    after = event["id"]
                    packet = f"id: {after}\nevent: deal\ndata: {encode(event)}\n\n".encode("utf-8")
                    self.wfile.write(packet)
                    self.wfile.flush()
                row = self.store.get_row(deal_id)
                if row is None or (row["status"] in {"completed", "failed"} and not events):
                    self.wfile.write(b"event: end\ndata: {}\n\n")
                    self.wfile.flush()
                    break
                if not events:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                time.sleep(0.75)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        path = urlparse(self.path).path
        if path != "/api/deals":
            return self._json(404, {"error": "not found"})
        try:
            payload = self._body()
        except (ValueError, json.JSONDecodeError) as error:
            return self._json(400, {"error": f"invalid request: {error}"})
        case_id = str(payload.get("case_id", ""))
        set_name = str(payload.get("set", "sandbox"))
        if set_name not in {"practice", "sandbox"} or not case_id:
            return self._json(400, {"error": "case_id and set=sandbox|practice are required"})
        active = [deal for deal in self.store.list_deals(set_name) if deal["status"] in {"queued", "running"}]
        if len(active) >= 3:
            return self._json(429, {"error": "Taktichek can run up to three dashboard deals at once"})
        if any(deal["case_id"] == case_id for deal in active):
            return self._json(409, {"error": "that case already has an active autonomous deal"})
        client = None
        try:
            client = ArenaClient()
            cases = client.cases(set_name)
            if set_name == "practice":
                cases = hosted(cases)
            case = next((item for item in cases if item.get("case_id") == case_id), None)
            if case is None:
                return self._json(404, {"error": "case is not available in this Arena set"})
            deal_id = self.store.create(case, set_name)
            self.store.emit(deal_id, "deal_queued", {"case": self.store.case_summary(case)})
            WORKERS.submit(run_deal, self.store, deal_id, case_id)
            detail = self.store.detail(deal_id)
            self.send_response(202)
            body = encode(detail).encode("utf-8")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except Exception as error:
            return self._json(503, {"error": f"Could not start autonomous deal: {error}"})
        finally:
            if client:
                client.close()


def main():
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    host = os.getenv("TAKTICHEK_HOST", "127.0.0.1")
    port = int(os.getenv("TAKTICHEK_PORT", "8765"))
    server = DashboardServer((host, port), DealStore())
    log.info("Taktichek dashboard listening at http://%s:%s", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        WORKERS.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
