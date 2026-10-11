"""Command line entry point for practice cases and live final waves."""

from __future__ import annotations

import argparse
import json
import logging

from dotenv import load_dotenv

from .analysis import summarize
from .agent import TaktikaAgent
from .client import ArenaClient
from .play import RemoteTable, hosted, play, play_all, play_waves
from .recording import save_results


def _client():
    return ArenaClient()


def _agent():
    # The runner creates one agent per case; no negotiation state is shared across sessions.
    return TaktikaAgent()


def _show(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _result_items(outcomes):
    if isinstance(outcomes, dict) and "score" in outcomes:
        return [(outcomes.get("case_id", "case"), outcomes)]
    if isinstance(outcomes, dict):
        return sorted(outcomes.items(), key=lambda item: str(item[0]))
    return []


def _report_results(directory, command, outcomes):
    items = _result_items(outcomes)
    scored, deals = [], 0
    for _case_id, result in items:
        if not isinstance(result, dict):
            continue
        score = result.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            scored.append(float(score) * (100 if abs(score) <= 1 else 1))
        deals += bool(result.get("deal"))

    try:
        saved = save_results(directory, command, outcomes)
    except OSError:
        logging.getLogger("taktika").exception("Could not save run results")
        saved = None
    location = f"saved: {saved}" if saved else f"save failed: {directory}"
    if len(items) == 1 and scored:
        score_text = f"SCORE: {scored[0]:.2f}% | {items[0][0]}"
    elif scored:
        score_text = (f"SCORE: mean {sum(scored) / len(scored):.2f}% | "
                      f"scored {len(scored)}/{len(items)} | deals {deals}/{len(items)}")
    else:
        score_text = f"SCORE: unavailable | scored 0/{len(items)} | deals {deals}/{len(items)}"
    print(f"{score_text} | {location}")


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(prog="taktika", description="Run the Taktika buyer agent")
    parser.add_argument("--verbose", action="store_true", help="show arena requests and negotiation progress")
    sub = parser.add_subparsers(dest="command", required=True)
    me = sub.add_parser("me", help="show team identity and remaining practice quota")
    cases = sub.add_parser("cases", help="list cases in a set")
    cases.add_argument("--set", default="practice", choices=("practice", "sandbox", "final"))
    one = sub.add_parser("play", help="play one case, for example case:P01")
    one.add_argument("case_id")
    one.add_argument("--record", metavar="DIR", default="runs",
                     help="save traces and result summaries under DIR (default: ./runs)")
    run = sub.add_parser("run", help="play hosted practice/sandbox cases or wait for final waves")
    run.add_argument("--set", default="practice", choices=("practice", "sandbox", "final"))
    run.add_argument("--parallel", type=int, default=8)
    run.add_argument("--limit", type=int, help="run at most this many hosted practice or sandbox cases")
    run.add_argument("--waves", type=int, default=5)
    run.add_argument("--record", metavar="DIR", default="runs",
                     help="save traces and result summaries under DIR (default: ./runs)")
    analyze = sub.add_parser("analyze", help="summarize local traces without connecting to Arena")
    analyze.add_argument("directory", metavar="DIR")
    analyze.add_argument("--replay", action="store_true",
                         help="compare recorded decisions with the current planner offline")
    for command in (me, cases, one, run, analyze):
        command.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS,
                             help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.command == "run":
        if args.limit is not None and args.limit < 1:
            parser.error("--limit must be at least 1")
        if args.limit is not None and args.set == "final":
            parser.error("--limit applies only to practice and sandbox runs")

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command == "analyze":
        _show(summarize(args.directory, replay=args.replay))
        return

    if args.command == "me":
        _show(_client().me())
    elif args.command == "cases":
        _show(_client().cases(args.set))
    elif args.command == "play":
        command = f"play {args.case_id}"
        try:
            result = play(RemoteTable(_client()), args.case_id, _agent(), record_dir=args.record)
            outcomes = {args.case_id: result}
        except Exception as error:
            outcomes = {args.case_id: error}
            _report_results(args.record, command, outcomes)
            raise SystemExit(1) from error
        _report_results(args.record, command, outcomes)
    elif args.set == "final":
        command = f"run --set final --waves {args.waves} --parallel {args.parallel}"
        try:
            outcomes = play_waves(_client(), _agent, waves=args.waves, parallel=args.parallel,
                                  record_dir=args.record)
        except Exception as error:
            outcomes = {"run": error}
            _report_results(args.record, command, outcomes)
            raise SystemExit(1) from error
        _report_results(args.record, command, outcomes)
    else:
        command = f"run --set {args.set} --parallel {args.parallel}"
        if args.limit is not None:
            command += f" --limit {args.limit}"
        try:
            client = _client()
            cases = client.cases(args.set)
            cases = hosted(cases) if args.set in {"practice", "sandbox"} else cases
            case_ids = [case["case_id"] for case in cases]
            if args.limit is not None:
                case_ids = case_ids[:args.limit]
            outcomes = play_all(lambda: RemoteTable(client), case_ids, _agent,
                                parallel=max(1, min(args.parallel, 10)), record_dir=args.record)
        except Exception as error:
            outcomes = {"run": error}
            _report_results(args.record, command, outcomes)
            raise SystemExit(1) from error
        _report_results(args.record, command, outcomes)


if __name__ == "__main__":
    main()
