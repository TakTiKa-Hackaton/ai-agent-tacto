"""MCP tools for browsing and exchanging supplier negotiation emails in the Arena."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Literal

from dotenv import load_dotenv
from mcp.server import MCPServer

from .agent import TaktikaAgent
from .client import ArenaClient
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
    messages = thread.get("messages", [])
    for message in messages:
        load_attachments(negotiation.table, message)
    negotiation.threads[supplier_id] = messages
    return thread


@server.tool()
def list_arena_cases(set_name: Literal["practice", "sandbox", "final"] = "practice") -> list[dict[str, Any]]:
    """List available Arena cases without starting a negotiation."""
    return tools().client.cases(set_name)


@server.tool()
def start_negotiation(case_id: str) -> dict[str, Any]:
    """Open an Arena case and return the suppliers' opening emails and session ID."""
    service = tools()
    opened = service.client.open_session(case_id)
    session_id = opened["session_id"]
    table = RemoteTable(service.client, opened["case"])
    table.session_id = session_id
    threads = opened["threads"]
    for messages in threads.values():
        for message in messages:
            load_attachments(table, message)
    negotiation = Negotiation(opened["case"], threads, table)
    with service.lock:
        service.sessions[session_id] = negotiation
    return {
        "session_id": session_id,
        "case_id": opened["case"].get("case_id", case_id),
        "title": opened["case"].get("title"),
        "suppliers": list(threads),
        "opening_emails": threads,
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
        if not live_threads(negotiation.threads):
            return {"status": "no_open_threads"}
        decision = TaktikaAgent().decide(negotiation.case, negotiation.threads)
        return decision


@server.tool()
def send_negotiation_email(
    session_id: str,
    supplier_id: str,
    subject: str,
    body: str,
    term_sheets: list[dict[str, Any]] | None = None,
    refs: list[str] | None = None,
) -> dict[str, Any]:
    """Send an email and up to three term sheets to a supplier in the Arena session."""
    negotiation = tools().get(session_id)
    if supplier_id not in negotiation.threads:
        raise ValueError("Supplier is not part of this case")
    term_sheets = term_sheets or []
    refs = refs or []
    if len(term_sheets) > 3:
        raise ValueError("term_sheets must contain at most three term sheets")
    if any(not isinstance(sheet, dict) or not isinstance(sheet.get("terms"), dict)
           for sheet in term_sheets):
        raise ValueError("Each term sheet must be an object with a terms object")
    if len(refs) > 20:
        raise ValueError("refs must contain at most 20 reference IDs")
    if len(subject) > 300 or len(body) > 8000:
        raise ValueError("subject is limited to 300 characters and body to 8000 characters")
    message = {
        "subject": subject,
        "body": body,
        "term_sheets": term_sheets,
        "refs": refs,
    }
    with negotiation.lock:
        _supplier_thread(tools(), negotiation, session_id, supplier_id)
        if supplier_id not in live_threads(negotiation.threads):
            raise ValueError("That supplier thread is already closed")
        reply = tools().client.send_message(session_id, supplier_id, message)
        if "result" in reply:
            negotiation.closed_result = reply["result"]
        else:
            negotiation.threads[supplier_id].extend([
                {**message, "from": "buyer"}, load_attachments(negotiation.table, reply),
            ])
        return {"buyer_email": message, "supplier_reply": reply}


@server.tool()
def close_negotiation(
    session_id: str,
    supplier_id: str,
    action: Literal["accept", "walk_away"],
    term_sheet_id: str = "",
) -> dict[str, Any]:
    """Accept the supplier's current offer or walk away, closing the Arena case."""
    negotiation = tools().get(session_id)
    if supplier_id not in negotiation.threads:
        raise ValueError("Supplier is not part of this case")
    with negotiation.lock:
        service = tools()
        _supplier_thread(service, negotiation, session_id, supplier_id)
        if supplier_id not in live_threads(negotiation.threads):
            raise ValueError("That supplier thread is already closed")
        if action == "accept":
            current_offer_id, _ = standing(negotiation.threads[supplier_id])
            if not term_sheet_id or term_sheet_id != current_offer_id:
                raise ValueError("To accept, supply the current standing term_sheet_id")
            payload = {"action": {"type": "accept", "term_sheet_id": term_sheet_id}}
        else:
            payload = {"action": {"type": "walk_away"}}
        result = service.client.send_message(session_id, supplier_id, payload)
        if "result" in result:
            negotiation.closed_result = result["result"]
            return negotiation.closed_result
        _supplier_thread(service, negotiation, session_id, supplier_id)
        if not live_threads(negotiation.threads):
            negotiation.closed_result = result
            return negotiation.closed_result
        return result


def main():
    load_dotenv()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "WARNING"))
    tools()
    server.run()


if __name__ == "__main__":
    main()
