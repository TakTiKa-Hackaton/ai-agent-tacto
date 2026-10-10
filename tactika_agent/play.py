"""Standalone session orchestration for hosted practice, sandbox, and final cases."""

from __future__ import annotations

import functools
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .client import ArenaError
from .cost_model import complete_offer, must_haves_ok, total_cost
from .document_reader import extract_text, supplier_standard_terms

log = logging.getLogger("taktika")
FINISHED = {"walked_away", "no_bid"}
RUNNING_SESSION = re.compile(r"one session per case in the final: (\S+)")


class RemoteTable:
    def __init__(self, client, case=None):
        self.client = client
        self.case = case
        self.session_id = None
        self.done = None

    def open(self, case_id):
        try:
            response = self.client.open_session(case_id)
        except ArenaError as error:
            match = RUNNING_SESSION.search(str(error.detail)) if error.status == 409 else None
            if not match or self.case is None:
                raise
            return self.resume(match.group(1))
        self.session_id = response["session_id"]
        return response["case"], response["threads"]

    def resume(self, session_id):
        self.session_id = session_id
        state = self.client.session(session_id)
        if state.get("status") == "ended":
            self.done = self.result()
        threads = {
            supplier: self.client.thread(session_id, supplier).get("messages", [])
            for supplier in state.get("threads", {})
        }
        return self.case, threads

    def send(self, supplier, message):
        return self.client.send_message(self.session_id, supplier, message)

    def close(self, supplier, action):
        reply = self.send(supplier, action)
        return reply.get("result", reply)

    def get_attachment(self, attachment_id):
        return self.client.attachment(self.session_id, attachment_id)

    def result(self):
        state = self.client.session(self.session_id)
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
            return {
                "from": "arena", "status": "walked_away" if thread.get("closed") else thread.get("status"),
                "term_sheets": sheets, "body": f"({error.detail})", "attachments": [],
            }
        messages = thread.get("messages", [])
        return messages[-1] if messages else {
            "from": "arena", "status": "walked_away", "term_sheets": [],
            "body": f"({error.detail})", "attachments": [],
        }


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


def moves_left(case, messages):
    return case.get("round_limit", 10) - sum(message.get("from") == "buyer" for message in messages)


def load_attachments(table, message):
    for attachment in message.get("attachments") or []:
        identifier = attachment.get("attachment_id")
        if identifier and "text" not in attachment:
            attachment["text"] = extract_text(attachment.get("name", "attachment"),
                                              table.get_attachment(identifier))
    return message


def close_decision(case, threads):
    alternatives = []
    baseline = (case.get("buyer", {}).get("alternative") or {}).get("terms") or {}
    if baseline.get("unit_price") is not None and must_haves_ok(case, baseline):
        alternatives.append(total_cost(case, complete_offer(case, baseline)))
    fallback = case["buyer"].get("fallback_unit_price")
    if fallback is not None:
        fallback_offer = complete_offer(case, {"unit_price": fallback})
        if must_haves_ok(case, fallback_offer):
            alternatives.append(total_cost(case, fallback_offer))
    cap = min(alternatives, default=float("inf"))
    choices = []
    for supplier in live_threads(threads):
        offer_id, terms = standing(threads[supplier])
        standard = supplier_standard_terms(threads[supplier])
        if offer_id and terms.get("unit_price") is not None and must_haves_ok(case, terms, standard):
            amount = total_cost(case, terms, standard)
            if amount <= cap + 0.01:
                choices.append((amount, supplier, offer_id))
    if choices:
        _, supplier, offer_id = min(choices)
        return {"supplier_id": supplier,
                "message": {"action": {"type": "accept", "term_sheet_id": offer_id}}}
    return {"supplier_id": live_threads(threads)[0], "message": {"action": {"type": "walk_away"}}}


def play(table, case_id, agent, deadline=None):
    case, threads = table.open(case_id)
    if table.done:
        return table.done
    for events in threads.values():
        for message in events:
            load_attachments(table, message)
    longest_turn = 0.0
    while live_threads(threads):
        start = time.monotonic()
        cutoff = deadline() if callable(deadline) else deadline
        if cutoff and time.time() > cutoff - max(90.0, longest_turn + 15.0):
            decision = close_decision(case, threads)
        else:
            decision = agent.decide(case, threads)
        supplier, message = decision["supplier_id"], decision["message"]
        if message.get("action"):
            try:
                return table.close(supplier, message)
            except ArenaError as error:
                if error.status != 409:
                    raise
                reply = table.after_conflict(supplier, error)
                if "result" in reply:
                    return reply["result"]
                threads[supplier].append(load_attachments(table, reply))
                continue
        try:
            reply = table.send(supplier, message)
        except ArenaError as error:
            if error.status != 409:
                raise
            reply = table.after_conflict(supplier, error)
            if "result" not in reply:
                threads[supplier].append(load_attachments(table, reply))
                continue
        if "result" in reply:
            return reply["result"]
        threads[supplier].extend([
            {**message, "from": "buyer"}, load_attachments(table, reply),
        ])
        longest_turn = max(longest_turn, time.monotonic() - start)
        log.info("%s: %s", supplier, reply.get("status"))
    return table.result()


def hosted(cases):
    if any("hosted" in case for case in cases):
        return [case for case in cases if case.get("hosted")]
    return cases


def play_all(new_table, case_ids, new_agent, parallel=8):
    quota_spent = threading.Event()

    def run_one(case_id):
        if quota_spent.is_set():
            return case_id, ArenaError(429, "practice quota reached; case not started")
        try:
            return case_id, play(new_table(), case_id, new_agent())
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


def play_waves(client, new_agent, waves=5, parallel=10, poll=5.0):
    futures, finished_waves, cutoffs = {}, set(), {}

    def start_case(case, cutoff):
        table = RemoteTable(client, case)
        return play(table, case["case_id"], new_agent(), functools.partial(cutoff.get, case["wave"]))

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
    return {case_id: future.result() for case_id, future in futures.items()}
