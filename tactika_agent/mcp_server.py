"""MCP tools for browsing and exchanging supplier negotiation emails in the Arena."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server import MCPServer

from .agent import TaktikaAgent
from .client import ArenaClient, ArenaError
from .cost_model import complete_offer, must_haves_ok
from .document_reader import supplier_standard_terms
from .operation_journal import OperationJournal
from .play import RemoteTable, load_attachments, live_threads, standing

log = logging.getLogger("taktika.mcp")
server = MCPServer("taktika-arena-email")


@dataclass
class Negotiation:
    case: dict[str, Any]
    threads: dict[str, list[dict[str, Any]]]
    table: RemoteTable
    lock: threading.RLock = field(default_factory=threading.RLock)
    closed_result: dict[str, Any] | None = None


class EmailTools:
    def __init__(self):
        self.client = ArenaClient()
        self.sessions: dict[str, Negotiation] = {}
        self.journal = OperationJournal(
            arena_url=os.getenv("ARENA_URL", ""), api_key=os.getenv("ARENA_API_KEY", "")
        )
        self.lock = threading.Lock()

    def get(self, session_id: str) -> Negotiation:
        with self.lock:
            negotiation = self.sessions.get(session_id)
        if negotiation is None:
            raise ValueError("Unknown session: start it with start_negotiation first")
        return negotiation


_tools: EmailTools | None = None
_tools_lock = threading.Lock()


def tools() -> EmailTools:
    global _tools
    with _tools_lock:
        if _tools is None:
            _tools = EmailTools()
    return _tools


def _supplier_thread(service: EmailTools, negotiation: Negotiation, session_id: str,
                     supplier_id: str) -> dict[str, Any]:
    """Refresh cached email events and extract any newly received attachments."""
    thread = service.client.thread(session_id, supplier_id)
    negotiation.threads[supplier_id] = negotiation.table._messages(thread)
    return thread


def _move_id(session_id: str, supplier_id: str, payload: dict[str, Any],
             operation_id: str | None = None) -> str:
    """Keep retries of one MCP operation on the same Arena move id."""
    identity = ["operation", operation_id] if operation_id is not None else ["payload", payload]
    encoded = json.dumps([session_id, supplier_id, identity], sort_keys=True,
                         separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def _restore_started_session(service: EmailTools, response: dict[str, Any], context: dict[str, Any] | None):
    if not context or response.get("session_id") in service.sessions:
        return
    case = context.get("case")
    if not case:
        return
    session_id = response.get("session_id")
    table = RemoteTable(service.client, case)
    table.session_id = session_id
    negotiation = Negotiation(case, context.get("threads", {}), table)
    with service.lock:
        service.sessions.setdefault(session_id, negotiation)


def _refresh_live_threads(service: EmailTools, negotiation: Negotiation, session_id: str,
                          include: tuple[str, ...] = ()) -> None:
    for supplier_id in set(live_threads(negotiation.threads)) | set(include):
        _supplier_thread(service, negotiation, session_id, supplier_id)


def _already_sent(negotiation: Negotiation, supplier_id: str,
                  message: dict[str, Any]) -> bool:
    def terms_fingerprint(sheets):
        return sorted(json.dumps(sheet.get("terms", {}), sort_keys=True,
                                 separators=(",", ":"), default=str)
                      for sheet in sheets)

    expected = (message.get("subject"), message.get("body"),
                terms_fingerprint(message.get("term_sheets", [])), message.get("refs", []))
    return any(
        event.get("from") == "buyer"
        and (event.get("subject"), event.get("body"),
             terms_fingerprint(event.get("term_sheets", [])),
             event.get("refs", [])) == expected
        for event in negotiation.threads.get(supplier_id, [])
    )


def _policy_context(negotiation: Negotiation):
    agent = TaktikaAgent()
    research = agent.researcher.collect(negotiation.case)
    return agent, research


def _validate_term_sheets(negotiation: Negotiation, supplier_id: str,
                          term_sheets: list[dict[str, Any]], agent, research) -> None:
    case = negotiation.case
    standard = supplier_standard_terms(negotiation.threads[supplier_id])
    cap = agent.planner.batna(case, negotiation.threads, supplier_id, research)[0]
    planned = agent.planner.offer_packages(case, negotiation.threads, supplier_id, research)
    allowed = set(case.get("negotiable", []))
    options = case.get("term_options", {})
    for sheet in term_sheets:
        terms = sheet["terms"]
        if set(terms) - allowed:
            raise ValueError("Term sheets may only contain negotiable case fields")
        price = terms.get("unit_price")
        if (isinstance(price, bool) or not isinstance(price, (int, float))
                or not math.isfinite(float(price)) or float(price) <= 0):
            raise ValueError("Each term sheet needs a positive finite unit_price")
        for field, value in terms.items():
            if options.get(field) and value not in options[field]:
                raise ValueError(f"{field} must use one of the case's allowed options")
        if not must_haves_ok(case, terms, standard):
            raise ValueError("Term sheet violates a buyer must-have")
        cost = agent.planner.cost(case, terms, standard, supplier_id, research)
        if cost > cap + 0.01:
            raise ValueError("Term sheet exceeds the best available alternative")
        specialist_candidate = (
            agent.council.enabled
            and agent.planner.is_safe_specialist_candidate(
                case, negotiation.threads, supplier_id, complete_offer(case, terms),
                research, planned,
            )
        )
        if complete_offer(case, terms) not in planned and not specialist_candidate:
            raise ValueError("Term sheet is outside Taktika's current negotiation plan")


def _validate_acceptance(negotiation: Negotiation, supplier_id: str,
                         term_sheet_id: str, agent, research) -> None:
    current_id, terms = standing(negotiation.threads[supplier_id])
    if not term_sheet_id or term_sheet_id != current_id:
        raise ValueError("To accept, supply the current standing term_sheet_id")
    standard = supplier_standard_terms(negotiation.threads[supplier_id])
    if terms.get("unit_price") is None or not must_haves_ok(negotiation.case, terms, standard):
        raise ValueError("The current offer violates a buyer must-have")
    cap = agent.planner.batna(negotiation.case, negotiation.threads, supplier_id, research)[0]
    state = agent.planner._state(negotiation.case, supplier_id,
                                 negotiation.threads[supplier_id], research)
    threshold = agent.planner.acceptance_threshold(
        negotiation.case, negotiation.threads[supplier_id], state, cap
    )
    if state["status"] == "agreed":
        threshold = cap
    if state["cost"] > cap + 0.01 or state["cost"] > threshold + 0.01:
        raise ValueError("The current offer is outside Taktika's acceptance threshold")


@server.tool()
def list_arena_cases(set_name: Literal["practice", "sandbox", "final"] = "practice") -> list[dict[str, Any]]:
    """List available Arena cases without starting a negotiation."""
    return tools().client.cases(set_name)


@server.tool()
def start_negotiation(case_id: str, operation_id: str | None = None) -> dict[str, Any]:
    """Open an Arena case; reuse operation_id if retrying a timed-out tool call."""
    service = tools()
    if operation_id is not None and (not isinstance(operation_id, str)
                                     or not operation_id.strip() or len(operation_id) > 200):
        raise ValueError("operation_id must be a non-empty string of at most 200 characters")
    scope = ("start", case_id)
    journal = None
    if operation_id:
        journal = service.journal.begin(
            scope, operation_id, {"case_id": case_id},
            _move_id("new", case_id, {"case_id": case_id}, operation_id),
        )
        if journal["status"] == "complete":
            _restore_started_session(service, journal["response"], journal["context"])
            return journal["response"]
    opened = service.client.open_session(case_id, move_id=journal["move_id"] if journal else None)
    session_id = opened["session_id"]
    table = RemoteTable(service.client, opened["case"])
    table.session_id = session_id
    threads = opened["threads"]
    negotiation = Negotiation(opened["case"], threads, table)
    with service.lock:
        service.sessions[session_id] = negotiation
    attachment_errors = []
    for supplier_id, messages in threads.items():
        for message in messages:
            try:
                load_attachments(table, message)
            except Exception:
                attachment_errors.append(supplier_id)
                log.exception("Could not process an opening attachment for %s", supplier_id)
                break
    response = {
        "session_id": session_id,
        "case_id": opened["case"].get("case_id", case_id),
        "title": opened["case"].get("title"),
        "suppliers": list(threads),
        "opening_emails": threads,
        "attachments_pending": sorted(set(attachment_errors)),
    }
    if journal:
        service.journal.complete(scope, operation_id, response,
                                 {"case": opened["case"], "threads": threads})
    return response


@server.tool()
def resume_negotiation(session_id: str, case: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resume a session using its Arena-provided or already cached case snapshot."""
    service = tools()
    with service.lock:
        negotiation = service.sessions.get(session_id)
    state = service.client.session(session_id)
    trusted_case = state.get("case") or (negotiation.case if negotiation else None)
    if trusted_case is None:
        raise ValueError("Cannot safely resume: Arena and this MCP process have no trusted case snapshot")
    if case is not None and case != trusted_case:
        raise ValueError("Supplied case snapshot does not match the trusted session case")
    case = trusted_case
    state_case_id = state.get("case_id") or (state.get("case") or {}).get("case_id")
    if state_case_id and case.get("case_id") != state_case_id:
        raise ValueError("Case snapshot does not match this Arena session")
    if negotiation is None:
        table = RemoteTable(service.client, case)
        table.session_id = session_id
        negotiation = Negotiation(case, {}, table)
        with service.lock:
            negotiation = service.sessions.setdefault(session_id, negotiation)
    else:
        table = negotiation.table
    suppliers = state.get("threads", {})
    with negotiation.lock:
        negotiation.case = case
        for supplier in suppliers:
            negotiation.threads[supplier] = table.thread_messages(supplier)
    if state.get("status") == "ended":
        negotiation.closed_result = state.get("result") or {
            "deal": False, "end_reason": state.get("end_reason"), "session_id": session_id,
        }
    return {
        "session_id": session_id,
        "case_id": case.get("case_id"),
        "status": state.get("status"),
        "suppliers": list(negotiation.threads),
        "threads": negotiation.threads,
        "result": negotiation.closed_result,
    }


@server.tool()
def read_supplier_email(session_id: str, supplier_id: str) -> dict[str, Any]:
    """Read a supplier's latest Arena email thread, including extracted attachment text."""
    negotiation = tools().get(session_id)
    if supplier_id not in negotiation.threads:
        raise ValueError("Supplier is not part of this case")
    with negotiation.lock:
        thread = _supplier_thread(tools(), negotiation, session_id, supplier_id)
        messages = negotiation.threads[supplier_id]
        return {
            "supplier_id": supplier_id,
            "status": thread.get("status"),
            "standing_offer_id": thread.get("offer_id"),
            "messages": messages,
        }


@server.tool()
def draft_negotiation_email(session_id: str) -> dict[str, Any]:
    """Ask Taktika to choose the next supplier email or closing action; this does not send it."""
    negotiation = tools().get(session_id)
    with negotiation.lock:
        if negotiation.closed_result:
            return {"status": "closed", "result": negotiation.closed_result}
        _refresh_live_threads(tools(), negotiation, session_id)
        if not live_threads(negotiation.threads):
            return {"status": "no_open_threads"}
        agent = TaktikaAgent()
        decision = agent.decide(negotiation.case, negotiation.threads)
        if agent.last_council:
            decision["decision_analysis"] = agent.last_council
        return decision


@server.tool()
def send_negotiation_email(
    session_id: str,
    supplier_id: str,
    subject: str,
    body: str,
    term_sheets: list[dict[str, Any]] | None = None,
    refs: list[str] | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Send an email and up to three term sheets; reuse operation_id for retries."""
    service = tools()
    if not isinstance(subject, str) or not isinstance(body, str):
        raise ValueError("subject and body must be strings")
    if not subject.strip() or not body.strip():
        raise ValueError("subject and body cannot be empty")
    if term_sheets is None:
        term_sheets = []
    if refs is None:
        refs = []
    if not isinstance(term_sheets, list) or len(term_sheets) > 3:
        raise ValueError("term_sheets must contain at most three term sheets")
    if any(not isinstance(sheet, dict) or not isinstance(sheet.get("terms"), dict)
           for sheet in term_sheets):
        raise ValueError("Each term sheet must be an object with a terms object")
    if not isinstance(refs, list) or len(refs) > 20:
        raise ValueError("refs must contain at most 20 reference IDs")
    if len(subject) > 300 or len(body) > 8000:
        raise ValueError("subject is limited to 300 characters and body to 8000 characters")
    if operation_id is not None and (not isinstance(operation_id, str)
                                     or not operation_id.strip() or len(operation_id) > 200):
        raise ValueError("operation_id must be a non-empty string of at most 200 characters")
    # Refs are set-like evidence identifiers; normalize order for idempotent retries.
    if any(not isinstance(ref, str) or not ref.strip() for ref in refs):
        raise ValueError("refs must contain non-empty reference ID strings")
    refs = sorted(set(refs))
    message = {
        "subject": subject,
        "body": body,
        "term_sheets": term_sheets,
        "refs": refs,
    }
    scope = ("send", session_id, supplier_id)
    journal = service.journal.lookup(scope, operation_id, message) if operation_id else None
    if journal and journal["status"] == "complete":
        return journal["response"]
    if journal and journal["status"] == "pending":
        try:
            reply = service.client.send_message(
                session_id, supplier_id, message, move_id=journal["move_id"]
            )
            response = {"buyer_email": message, "supplier_reply": reply}
        except ArenaError as error:
            if error.status != 409:
                raise
            response = {"status": "conflict", "detail": error.detail,
                        "session_id": session_id, "supplier_id": supplier_id}
        service.journal.complete(scope, operation_id, response)
        return response
    negotiation = service.get(session_id)
    if supplier_id not in negotiation.threads:
        raise ValueError("Supplier is not part of this case")
    with negotiation.lock:
        if negotiation.closed_result:
            return {"status": "closed", "result": negotiation.closed_result}
        _refresh_live_threads(tools(), negotiation, session_id, (supplier_id,))
        if operation_id is None and _already_sent(negotiation, supplier_id, message):
            return {"status": "already_sent", "messages": negotiation.threads[supplier_id]}
        if supplier_id not in live_threads(negotiation.threads):
            raise ValueError("That supplier thread is already closed")
        if term_sheets or refs:
            agent, research = _policy_context(negotiation)
            if term_sheets:
                _validate_term_sheets(negotiation, supplier_id, term_sheets, agent, research)
            if any(not agent.researcher.valid_ref(negotiation.case, ref) for ref in refs):
                raise ValueError("Each evidence reference must resolve in the case or research package")
        journal = service.journal.begin(
            scope, operation_id, message,
            _move_id(session_id, supplier_id, message, operation_id),
        ) if operation_id else None
        if journal and journal["status"] == "complete":
            return journal["response"]
        try:
            reply = tools().client.send_message(
                session_id, supplier_id, message,
                move_id=journal["move_id"] if journal else _move_id(session_id, supplier_id, message)
            )
        except ArenaError as error:
            if error.status != 409:
                raise
            thread = _supplier_thread(tools(), negotiation, session_id, supplier_id)
            response = {"status": "conflict", "detail": error.detail, "supplier_thread": thread,
                        "messages": negotiation.threads[supplier_id]}
            if journal:
                service.journal.complete(scope, operation_id, response)
            return response
        if "result" in reply:
            negotiation.closed_result = reply["result"]
        else:
            negotiation.threads[supplier_id].extend([
                {**message, "from": "buyer"}, load_attachments(negotiation.table, reply),
            ])
        response = {"buyer_email": message, "supplier_reply": reply}
        if journal:
            service.journal.complete(scope, operation_id, response)
        return response


@server.tool()
def close_negotiation(
    session_id: str,
    supplier_id: str,
    action: Literal["accept", "walk_away"],
    term_sheet_id: str = "",
    operation_id: str | None = None,
) -> dict[str, Any]:
    """Accept or walk away; reuse operation_id if retrying a timed-out tool call."""
    service = tools()
    if action not in {"accept", "walk_away"}:
        raise ValueError("action must be 'accept' or 'walk_away'")
    if operation_id is not None and (not isinstance(operation_id, str)
                                     or not operation_id.strip() or len(operation_id) > 200):
        raise ValueError("operation_id must be a non-empty string of at most 200 characters")
    payload = ({"action": {"type": "accept", "term_sheet_id": term_sheet_id}}
               if action == "accept" else {"action": {"type": "walk_away"}})
    scope = ("close", session_id, supplier_id)
    saved = service.journal.lookup(scope, operation_id, payload) if operation_id else None
    if saved and saved["status"] == "complete":
        return saved["response"]
    if saved and saved["status"] == "pending":
        try:
            arena_reply = service.client.send_message(
                session_id, supplier_id, payload, move_id=saved["move_id"]
            )
            response = arena_reply.get("result", arena_reply)
            if "result" in arena_reply:
                with service.lock:
                    negotiation = service.sessions.get(session_id)
                if negotiation:
                    negotiation.closed_result = response
        except ArenaError as error:
            if error.status != 409:
                raise
            response = {"status": "conflict", "detail": error.detail,
                        "session_id": session_id, "supplier_id": supplier_id}
        service.journal.complete(scope, operation_id, response)
        return response
    negotiation = service.get(session_id)
    if supplier_id not in negotiation.threads:
        raise ValueError("Supplier is not part of this case")
    with negotiation.lock:
        if negotiation.closed_result:
            return negotiation.closed_result
        _refresh_live_threads(service, negotiation, session_id, (supplier_id,))
        if supplier_id not in live_threads(negotiation.threads):
            state = service.client.session(session_id)
            if state.get("status") == "ended":
                negotiation.closed_result = state.get("result") or {
                    "deal": False, "end_reason": state.get("end_reason"), "session_id": session_id,
                }
                return negotiation.closed_result
            return {"status": "already_closed", "supplier_id": supplier_id,
                    "thread": negotiation.threads[supplier_id]}
        if action == "accept":
            agent, research = _policy_context(negotiation)
            _validate_acceptance(negotiation, supplier_id, term_sheet_id, agent, research)
        journal = service.journal.begin(
            scope, operation_id, payload,
            _move_id(session_id, supplier_id, payload, operation_id),
        ) if operation_id else None
        if journal and journal["status"] == "complete":
            return journal["response"]
        try:
            result = service.client.send_message(
                session_id, supplier_id, payload,
                move_id=journal["move_id"] if journal else _move_id(session_id, supplier_id, payload),
            )
        except ArenaError as error:
            if error.status != 409:
                raise
            thread = _supplier_thread(service, negotiation, session_id, supplier_id)
            response = {"status": "conflict", "detail": error.detail, "supplier_thread": thread,
                        "messages": negotiation.threads[supplier_id]}
            if journal:
                service.journal.complete(scope, operation_id, response)
            return response
        if "result" in result:
            negotiation.closed_result = result["result"]
            response = negotiation.closed_result
        else:
            _supplier_thread(service, negotiation, session_id, supplier_id)
            if not live_threads(negotiation.threads):
                negotiation.closed_result = result
                response = negotiation.closed_result
            else:
                response = result
        if journal:
            service.journal.complete(scope, operation_id, response)
        return response


def main():
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING"))
    tools()
    server.run()


if __name__ == "__main__":
    main()
