"""Standalone session orchestration for hosted practice, sandbox, and final cases."""

from __future__ import annotations

import functools
import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .client import ArenaError
from .document_reader import extract_text
from .recording import RunRecorder

log = logging.getLogger("taktika")
# "agreed" means the supplier accepted the buyer's offer; the buyer still has
# to send the explicit accept action before the Arena closes the deal.
FINISHED = {"walked_away", "no_bid", "closed", "ended"}
RUNNING_SESSION = re.compile(r"one session per case in the final: (\S+)")


class RemoteTable:
    def __init__(self, client, case=None):
        self.client = client
        self.case = case
        self.session_id = None
        self.done = None

    def open(self, case_id):
        if self.session_id:
            return self.resume(self.session_id)
        try:
            response = self.client.open_session(case_id)
        except ArenaError as error:
            match = RUNNING_SESSION.search(str(error.detail)) if error.status == 409 else None
            if not match or self.case is None:
                raise
            return self.resume(match.group(1))
        self.session_id = response["session_id"]
        self.case = response["case"]
        return self.case, {
            supplier: self._messages({"messages": messages})
            for supplier, messages in response["threads"].items()
        }

    def resume(self, session_id):
        self.session_id = session_id
        state = self.client.session(session_id)
        self.case = state.get("case") or self.case
        if state.get("status") == "ended":
            self.done = self.result()
        threads = {
            supplier: self._messages(self.client.thread(session_id, supplier))
            for supplier in state.get("threads", {})
        }
        return self.case, threads

    def _messages(self, thread):
        messages = [load_attachments(self, message) for message in thread.get("messages", [])]
        status = thread.get("status")
        if thread.get("closed") and status not in FINISHED:
            status = "walked_away"
        if messages and status in FINISHED:
            if messages[-1].get("from") == "buyer":
                messages.append({"from": "arena", "status": status,
                                 "term_sheets": [], "attachments": []})
            else:
                messages[-1] = {**messages[-1], "status": status}
        elif not messages and status in FINISHED:
            messages = [{"from": "arena", "status": status, "term_sheets": [], "attachments": []}]
        return messages

    def thread_messages(self, supplier):
        return self._messages(self.client.thread(self.session_id, supplier))

    def send(self, supplier, message):
        return self.client.send_message(self.session_id, supplier, message)

    def close(self, supplier, action):
        return self.send(supplier, action)

    def get_attachment(self, attachment_id):
        return self.client.attachment(self.session_id, attachment_id)

    def result(self):
        state = self.client.session(self.session_id)
        return state.get("result") or {
            "deal": False, "end_reason": state.get("end_reason"), "session_id": self.session_id,
        }

    def ended_result(self):
        state = self.client.session(self.session_id)
        if state.get("status") != "ended":
            return None
        return state.get("result") or {
            "deal": False, "end_reason": state.get("end_reason"), "session_id": self.session_id,
        }

    def after_conflict(self, supplier, error):
        state = self.client.session(self.session_id)
        if state.get("status") == "ended":
            return {"result": state.get("result") or {"deal": False, "end_reason": state.get("end_reason")}}
        thread = self.client.thread(self.session_id, supplier)
        timeouts = thread.get("timeouts") or []
        if timeouts:
            latest_timeout = timeouts[-1]
            sheet_id = thread.get("offer_id")
            sheets = ([{"term_sheet_id": sheet_id, "terms": latest_timeout.get("terms", {})}]
                      if sheet_id else [])
            events = self._messages(thread)
            events.append({
                "from": "arena", "status": "walked_away" if thread.get("closed") else thread.get("status"),
                "term_sheets": sheets, "body": f"({error.detail})", "attachments": [],
            })
            return {"thread_events": events}
        events = self._messages(thread)
        if events:
            return {"thread_events": events}
        return {"thread_events": [{
            "from": "arena", "status": "walked_away", "term_sheets": [],
            "body": f"({error.detail})", "attachments": [],
        }]}


def latest(messages):
    return next(message for message in reversed(messages) if message.get("from") != "buyer")


def standing(messages):
    sheets = latest(messages).get("term_sheets") or []
    if not sheets:
        return None, {}
    first = sheets[0]
    return first.get("term_sheet_id"), first.get("terms", {})


def live_threads(threads):
    result = []
    for supplier, messages in threads.items():
        if messages and latest(messages).get("status") not in FINISHED:
            result.append(supplier)
    return result


def _thread_state(threads):
    """Fingerprint the canonical supplier state when handling a 409 conflict."""
    return tuple(sorted(
        (supplier, len(events), latest(events).get("status") if events else None,
         standing(events)[0] if events else None)
        for supplier, events in threads.items()
    ))


def moves_left(case, messages):
    return case.get("round_limit", 10) - sum(message.get("from") == "buyer" for message in messages)


def load_attachments(table, message):
    for attachment in message.get("attachments") or []:
        identifier = attachment.get("attachment_id")
        if identifier and "text" not in attachment:
            try:
                attachment["text"] = extract_text(attachment.get("name", "attachment"),
                                                  table.get_attachment(identifier))
            except Exception as error:
                attachment["text_unavailable"] = type(error).__name__
                log.warning("Could not extract supplier attachment %s: %s", identifier, error)
    return message


def close_decision(case, threads, agent=None):
    planner_decision = getattr(agent, "close_decision", None)
    if planner_decision:
        return planner_decision(case, threads)
    planner = getattr(agent, "planner", None)
    researcher = getattr(agent, "researcher", None)
    if planner is not None and researcher is not None:
        return planner.close_decision(case, threads, researcher.collect(case))
    raise TypeError("deadline decisions require a research-aware planner")


def _finish(record, result):
    if record:
        record.finish(result)
    return result


def _abort(record, error):
    if record:
        record.fail(error)
    raise error


def play(table, case_id, agent, deadline=None, record_dir=None):
    case, threads = table.open(case_id)
    try:
        record = RunRecorder(record_dir, case_id, table.session_id, case, threads) if record_dir else None
    except OSError:
        log.exception("Could not initialize local negotiation trace; continuing without recording")
        record = None
    if table.done:
        return _finish(record, table.done)
    for events in threads.values():
        for message in events:
            load_attachments(table, message)
    longest_turn = 0.0
    conflicted_closes = set()
    conflicted_messages = set()
    while live_threads(threads):
        start = time.monotonic()
        cutoff = deadline() if callable(deadline) else deadline
        if cutoff and time.time() > cutoff - max(90.0, longest_turn + 15.0):
            decision = close_decision(case, threads, agent)
            mode = "deadline"
        else:
            decision = agent.decide(case, threads)
            mode = "normal"
        if record:
            record.decision(threads, getattr(agent, "last_research", None), decision, mode,
                            getattr(agent, "last_council", None))
        supplier, message = decision["supplier_id"], decision["message"]
        if message.get("action"):
            before_events = threads.get(supplier, [])
            before = (len(before_events),
                      latest(before_events).get("status") if before_events else None,
                      standing(before_events)[0] if before_events else None)
            case_state = _thread_state(threads)
            action = message["action"]
            conflict_key = (case_state, supplier, action.get("type"), action.get("term_sheet_id"))
            if conflict_key in conflicted_closes:
                _abort(record, RuntimeError(
                    "Arena repeatedly rejected the same closing action without a state change"
                ))
            try:
                reply = table.close(supplier, message)
            except ArenaError as error:
                if record:
                    record.response({"error": {"status": error.status, "detail": error.detail}})
                if error.status != 409:
                    _abort(record, error)
                reply = table.after_conflict(supplier, error)
                if "result" in reply:
                    if record:
                        record.response(reply)
                    return _finish(record, reply["result"])
                if record:
                    record.response(reply)
                threads[supplier] = reply["thread_events"]
                for other in live_threads(threads):
                    if other != supplier:
                        threads[other] = table.thread_messages(other)
                conflicted_closes.add(conflict_key)
                continue
            else:
                if record:
                    record.response(reply)
                if "result" in reply:
                    return _finish(record, reply["result"])
                ended = table.ended_result()
                if ended is not None:
                    return _finish(record, ended)
                threads[supplier] = table.thread_messages(supplier)
                if action.get("type") == "accept":
                    if latest(threads[supplier]).get("status") == "agreed":
                        _abort(record, RuntimeError(
                            "Arena marked the offer agreed but did not return a session result"
                        ))
                    _abort(record, RuntimeError("Arena acknowledged acceptance without a terminal result"))
            current = threads[supplier]
            after = (len(current), latest(current).get("status") if current else None,
                     standing(current)[0] if current else None)
            if before == after and supplier in live_threads(threads):
                _abort(record, RuntimeError(
                    "Arena did not report a thread state change after the closing action"
                ))
            if not live_threads(threads):
                return _finish(record, table.result())
            conflicted_closes.clear()
            conflicted_messages.clear()
            continue
        state_before = _thread_state(threads)
        message_key = (state_before, supplier,
                       json.dumps(message, sort_keys=True, separators=(",", ":"), default=str))
        if message_key in conflicted_messages:
            _abort(record, RuntimeError("Arena repeatedly rejected the same email without a state change"))
        try:
            reply = table.send(supplier, message)
        except ArenaError as error:
            if record:
                record.response({"error": {"status": error.status, "detail": error.detail}})
            if error.status != 409:
                _abort(record, error)
            reply = table.after_conflict(supplier, error)
            if "result" not in reply:
                if record:
                    record.response(reply)
                threads[supplier] = reply["thread_events"]
                for other in live_threads(threads):
                    if other != supplier:
                        threads[other] = table.thread_messages(other)
                if _thread_state(threads) == state_before:
                    conflicted_messages.add(message_key)
                continue
        if "result" in reply:
            if record:
                record.response(reply)
            return _finish(record, reply["result"])
        if record:
            record.response(reply)
        threads[supplier].extend([
            {**message, "from": "buyer"}, load_attachments(table, reply),
        ])
        conflicted_closes.clear()
        conflicted_messages.clear()
        longest_turn = max(longest_turn, time.monotonic() - start)
        log.info("%s: %s", supplier, reply.get("status"))
    return _finish(record, table.result())


def hosted(cases):
    if any("hosted" in case for case in cases):
        return [case for case in cases if case.get("hosted")]
    return cases


def play_all(new_table, case_ids, new_agent, parallel=8, record_dir=None):
    quota_spent = threading.Event()

    def run_one(case_id):
        if quota_spent.is_set():
            return case_id, ArenaError(429, "practice quota reached; case not started")
        try:
            return case_id, play(new_table(), case_id, new_agent(), record_dir=record_dir)
        except ArenaError as error:
            if error.status == 429 and str(error.detail).startswith("practice limit"):
                quota_spent.set()
            log.exception("case %s could not finish", case_id)
            return case_id, error
        except Exception as error:
            log.exception("case %s failed", case_id)
            return case_id, error

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        return dict(pool.map(run_one, case_ids))


def play_waves(client, new_agent, waves=5, parallel=10, poll=5.0, record_dir=None):
    futures, finished_waves, cutoffs = {}, set(), {}

    def start_case(case, cutoff):
        table = RemoteTable(client, case)
        return play(table, case["case_id"], new_agent(), functools.partial(cutoff.get, case["wave"]),
                    record_dir=record_dir)

    with ThreadPoolExecutor(max_workers=max(1, min(parallel, 10))) as pool:
        while len(finished_waves) < waves or any(not future.done() for future in futures.values()):
            try:
                active = client.cases("final")
            except ArenaError as error:
                if error.status != 403:
                    raise
                active = []
            for case in active:
                if not case.get("open"):
                    finished_waves.add(case["wave"])
                    continue
                remaining = case.get("seconds_left")
                if remaining is not None:
                    cutoffs[case["wave"]] = time.time() + remaining
                if case["case_id"] not in futures and remaining:
                    futures[case["case_id"]] = pool.submit(start_case, case, cutoffs)
            time.sleep(poll)
    results = {}
    for case_id, future in futures.items():
        try:
            results[case_id] = future.result()
        except Exception as error:
            log.exception("final case %s failed", case_id)
            results[case_id] = error
    return results
