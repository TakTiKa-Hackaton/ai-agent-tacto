"""Command line entry point for practice cases and live final waves."""

from __future__ import annotations

import argparse
import json
import logging

from arena_kit.client import ArenaClient
from arena_kit.play import RemoteTable, hosted, play, play_all, play_waves
from dotenv import load_dotenv

from .agent import TaktikaAgent


def _client():
    return ArenaClient()


def _agent():
    # The runner creates one agent per case; no negotiation state is shared across sessions.
    return TaktikaAgent()


def _show(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def main(argv=None):
    load_dotenv()
    parser = argparse.ArgumentParser(prog="taktika", description="Run the Taktika buyer agent")
    parser.add_argument("--verbose", action="store_true", help="show arena requests and negotiation progress")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("me", help="show team identity and remaining practice quota")
    cases = sub.add_parser("cases", help="list cases in a set")
    cases.add_argument("--set", default="practice", choices=("practice", "sandbox", "final"))
    one = sub.add_parser("play", help="play one case, for example case:P01")
    one.add_argument("case_id")
    run = sub.add_parser("run", help="play hosted practice/sandbox cases or wait for final waves")
    run.add_argument("--set", default="practice", choices=("practice", "sandbox", "final"))
    run.add_argument("--parallel", type=int, default=8)
    run.add_argument("--waves", type=int, default=5)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    client = _client()
    if args.command == "me":
        _show(client.me())
    elif args.command == "cases":
        _show(client.cases(args.set))
    elif args.command == "play":
        _show(play(RemoteTable(client), args.case_id, _agent()))
    elif args.set == "final":
        _show(play_waves(client, _agent, waves=args.waves, parallel=args.parallel))
    else:
        cases = client.cases(args.set)
        cases = hosted(cases) if args.set in {"practice", "sandbox"} else cases
        _show(play_all(lambda: RemoteTable(client), [case["case_id"] for case in cases], _agent,
                       parallel=max(1, min(args.parallel, 10))))


if __name__ == "__main__":
    main()
