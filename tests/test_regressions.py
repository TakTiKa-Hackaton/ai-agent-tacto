"""Offline regression fixtures. These tests never contact the Arena or a model API."""

from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from tactika_agent.analysis import summarize
from tactika_agent.agent import _research_root
from tactika_agent.operation_journal import OperationJournal
from tactika_agent.play import live_threads, play
from tactika_agent.recording import RunRecorder, save_results
from tactika_agent.runner import _report_results, main as runner_main
from tactika_agent.strategy import OfferPlanner


class ProtocolRegressionTests(unittest.TestCase):
    def test_practice_batch_limit_selects_no_more_than_requested(self):
        class Client:
            @staticmethod
            def cases(_set):
                return [{"case_id": f"case:P{index:02d}"} for index in range(1, 9)]

        with patch("tactika_agent.runner.load_dotenv"), \
                patch("tactika_agent.runner._client", return_value=Client()), \
                patch("tactika_agent.runner.hosted", side_effect=lambda cases: cases), \
                patch("tactika_agent.runner.play_all", return_value={}) as play_all, \
                patch("tactika_agent.runner._report_results"):
            runner_main(["run", "--set", "practice", "--limit", "6"])

        self.assertEqual(play_all.call_args.args[1], [f"case:P{index:02d}" for index in range(1, 7)])

    def test_agreed_thread_stays_live_until_buyer_accepts(self):
        threads = {"supplier-1": [{"from": "supplier", "status": "agreed"}]}
        self.assertEqual(live_threads(threads), ["supplier-1"])

    def test_play_sends_accept_after_supplier_agrees(self):
        class Table:
            session_id = "offline-session"
            done = None

            def __init__(self):
                self.actions = []

            def open(self, _case_id):
                return {"case_id": "case:P01"}, {
                    "supplier-1": [{"from": "supplier", "status": "negotiating",
                                     "term_sheets": []}],
                }

            def send(self, _supplier, _message):
                return {"from": "supplier", "status": "agreed", "term_sheets": [
                    {"term_sheet_id": "offer-1", "terms": {"unit_price": 9.0}},
                ]}

            def close(self, _supplier, message):
                self.actions.append(message["action"]["type"])
                return {"result": {"deal": True, "score": 100}}

            def result(self):
                return {"deal": False}

            def ended_result(self):
                return None

            def thread_messages(self, _supplier):
                return [{"from": "supplier", "status": "agreed", "term_sheets": [
                    {"term_sheet_id": "offer-1", "terms": {"unit_price": 9.0}},
                ]}]

        class Agent:
            def __init__(self):
                self.turn = 0

            def decide(self, _case, threads):
                self.turn += 1
                if self.turn == 1:
                    return {"supplier_id": "supplier-1", "message": {"subject": "Offer",
                            "body": "Terms", "term_sheets": []}}
                self.assert_agreed = threads["supplier-1"][-1]["status"] == "agreed"
                return {"supplier_id": "supplier-1", "message": {"action": {
                    "type": "accept", "term_sheet_id": "offer-1",
                }}}

        table, agent = Table(), Agent()
        result = play(table, "case:P01", agent)
        self.assertEqual(result["deal"], True)
        self.assertEqual(table.actions, ["accept"])
        self.assertTrue(agent.assert_agreed)


class StrategyRegressionTests(unittest.TestCase):
    def test_saved_sessions_replay_without_deterministic_decision_changes(self):
        runs = Path(__file__).resolve().parents[1] / "runs"
        result = summarize(runs, replay=True)
        self.assertEqual(result["divergence_count"], 0)

    def test_missing_batna_does_not_turn_terminal_offer_into_unlimited_acceptance(self):
        planner = OfferPlanner(None)
        state = {"status": "final_offer", "target_cost": 42.5}
        self.assertEqual(planner.acceptance_threshold({}, [], state, float("inf")), 42.5)

    def test_price_optimizer_returns_no_offer_if_no_price_fits_cap(self):
        case = {
            "buyer": {
                "freight_eur_per_unit": {"EXW": 0},
                "need_per_year": 1,
                "horizon_years": 1,
                "reference_payment_days": 0,
                "need_lead_time_days": 1,
                "fallback_unit_price": 100,
                "interest_rate": 0,
            },
            "negotiable": ["unit_price"],
            "term_options": {"unit_price": [5, 6]},
        }
        planner = OfferPlanner(None)
        self.assertIsNone(planner._max_price(case, {}, {}, "supplier-1", 4, {}))


class PersistenceRegressionTests(unittest.TestCase):
    def test_research_path_is_project_relative_and_falls_back_to_bundled_data(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory) / "agent"
            bundled = project / "data"
            bundled.mkdir(parents=True)
            self.assertEqual(_research_root("data", project).resolve(), bundled.resolve())
            self.assertEqual(_research_root("../missing-data", project).resolve(), bundled.resolve())

    def test_result_summary_saves_all_outcomes_and_prints_one_score_line(self):
        with tempfile.TemporaryDirectory() as directory:
            outcomes = {
                "case:P03": {"deal": True, "score": 0.899833878},
                "case:P07": RuntimeError("offline failure"),
            }
            output = io.StringIO()
            with redirect_stdout(output):
                _report_results(directory, "run --set practice", outcomes)
            lines = output.getvalue().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertTrue(lines[0].startswith("SCORE: mean 89.98%"))
            saved_path = next(Path(directory).glob("results-*.json"))
            saved = json.loads(saved_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["outcomes"]["case:P03"]["score"], 0.899833878)
            self.assertEqual(saved["outcomes"]["case:P07"]["error"]["type"], "RuntimeError")

    def test_operation_journal_replays_completed_call_and_rejects_payload_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = OperationJournal(directory, "https://arena.invalid", "key")
            journal.begin(("send", "session", "supplier"), "op-1", {"body": "hello"}, "move-1")
            journal.complete(("send", "session", "supplier"), "op-1", {"reply": "ok"})
            reopened = OperationJournal(directory, "https://arena.invalid", "key")
            saved = reopened.lookup(("send", "session", "supplier"), "op-1", {"body": "hello"})
            self.assertEqual(saved["status"], "complete")
            self.assertEqual(saved["response"], {"reply": "ok"})
            with self.assertRaises(ValueError):
                reopened.lookup(("send", "session", "supplier"), "op-1", {"body": "different"})

    def test_trace_is_analyzable_and_stores_a_snapshot(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "tactika_agent.recording.git_revision", return_value="offline-revision"
        ):
            recorder = RunRecorder(directory, "case:P01", "session-1", {"title": "Case"}, {})
            mutable_threads = {"supplier-1": [{"status": "negotiating"}]}
            recorder.decision(mutable_threads, {}, {"supplier_id": "supplier-1", "message": {}}, "normal")
            mutable_threads["supplier-1"][0]["status"] = "changed"
            recorder.finish({"deal": True, "score": 90})
            summary = summarize(directory)
            trace_path = next(Path(directory).glob("*.json"))
            trace = json.loads(trace_path.read_text(encoding="utf-8"))
            self.assertEqual(trace["decisions"][0]["threads"]["supplier-1"][0]["status"], "negotiating")
            self.assertEqual(summary["completed_count"], 1)
            self.assertEqual(summary["mean_score"], 90)

    def test_saved_rejection_forecast_is_scored_against_the_supplier_reply(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "tactika_agent.recording.git_revision", return_value="offline-revision"
        ):
            recorder = RunRecorder(directory, "case:P01", "session-forecast",
                                   {"title": "Forecast case"}, {})
            council = {
                "selected_candidate_ids": ["0"],
                "rejection_risk": {"0": {"accept": 0.7, "counter": 0.2,
                                           "supplier_walkaway": 0.1}},
            }
            recorder.decision({}, {}, {"supplier_id": "supplier-1",
                                       "message": {"term_sheets": [{"terms": {"unit_price": 10}}]}},
                              "normal", council)
            recorder.response({"from": "supplier", "status": "open",
                               "term_sheets": [{"term_sheet_id": "offer-1",
                                                "terms": {"unit_price": 9}}]})
            recorder.finish({"deal": True, "score": 90})

            risk = summarize(directory)["rejection_risk"]

            self.assertEqual(risk["scored_count"], 1)
            self.assertEqual(risk["outcomes"], {"counter": 1})
            self.assertEqual(risk["multiclass_brier_score"], 1.14)


if __name__ == "__main__":
    unittest.main()
