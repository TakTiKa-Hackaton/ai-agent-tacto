"""Offline summaries and decision replay for locally recorded Arena sessions."""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

from .research import ProcurementResearch
from .strategy import OfferPlanner


def _traces(location):
    path = Path(location).expanduser()
    candidates = [path] if path.is_file() else sorted(path.rglob("*.json"))
    traces = []
    for candidate in candidates:
        try:
            trace = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if trace.get("format_version") == 1:
            traces.append((candidate, trace))
    return traces


def _choice(proposal):
    message = proposal.get("message") or {}
    return {
        "supplier_id": proposal.get("supplier_id"),
        "action": message.get("action"),
        "term_sheets": message.get("term_sheets", []),
    }


def _observed_outcome(turn):
    replies = turn.get("responses", [])
    reply = next((item for item in reversed(replies)
                  if isinstance(item, dict) and item.get("status")), None)
    if not reply:
        return None
    status = str(reply["status"]).lower()
    if status in {"agreed", "accepted"}:
        return "accept"
    if status in {"walked_away", "walkaway", "rejected", "withdrawn", "supplier_walkaway"}:
        return "supplier_walkaway"
    if status in {"open", "final_offer", "negotiating"} and reply.get("term_sheets"):
        return "counter"
    return None


def _rejection_summary(traces):
    outcomes = Counter()
    scores = []
    prediction_count = ambiguous_bundle_count = 0
    for _, trace in traces:
        for turn in trace.get("decisions", []):
            council = turn.get("council") or {}
            selected = council.get("selected_candidate_ids") or []
            forecasts = council.get("rejection_risk") or {}
            if len(selected) > 1:
                ambiguous_bundle_count += 1
                continue
            if len(selected) != 1 or str(selected[0]) not in forecasts:
                continue
            prediction_count += 1
            observed = _observed_outcome(turn)
            if not observed:
                continue
            forecast = forecasts[str(selected[0])]
            names = ("accept", "counter", "supplier_walkaway")
            probabilities = [forecast.get(name) for name in names]
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   or not math.isfinite(value) for value in probabilities):
                continue
            outcomes[observed] += 1
            scores.append(sum((probability - (1.0 if name == observed else 0.0)) ** 2
                              for name, probability in zip(names, probabilities)))
    return {
        "prediction_count": prediction_count,
        "scored_count": len(scores),
        "ambiguous_bundle_count": ambiguous_bundle_count,
        "outcomes": dict(outcomes),
        "multiclass_brier_score": round(sum(scores) / len(scores), 8) if scores else None,
        "note": "Descriptive only; small samples do not establish probability calibration.",
    }


class _ReplayWriter:
    @staticmethod
    def draft(*_args, **_kwargs):
        return ""


def summarize(location, replay=False):
    traces = _traces(location)
    score_values, rounds, failures = [], [], Counter()
    deals = completed = 0
    cases, divergences = [], []
    goal_results = {}
    council_statuses, council_failures = Counter(), Counter()
    supplier_health_cost, reputation_metric = Counter(), Counter()
    planner = OfferPlanner(ProcurementResearch(None)) if replay else None

    for path, trace in traces:
        result = trace.get("result") or {}
        complete = trace.get("status") == "complete"
        completed += complete
        deals += bool(result.get("deal"))
        score = result.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            score_values.append(float(score))
        goal = (trace.get("case") or {}).get("buyer", {}).get("category_goal")
        if goal:
            aggregate = goal_results.setdefault(goal, {
                "case_count": 0, "completed_count": 0, "deal_count": 0,
                "score_sum": 0.0, "scored_count": 0,
            })
            aggregate["case_count"] += 1
            aggregate["completed_count"] += complete
            aggregate["deal_count"] += bool(result.get("deal"))
            if isinstance(score, (int, float)) and not isinstance(score, bool):
                aggregate["score_sum"] += float(score)
                aggregate["scored_count"] += 1
        rounds.append(len(trace.get("decisions", [])))
        if trace.get("error"):
            failures[trace["error"].get("type", "unknown")] += 1
        if result.get("must_haves_ok") is False:
            failures["must_have_violation"] += 1
        cases.append({
            "file": str(path), "case_id": trace.get("case_id"),
            "status": trace.get("status"), "deal": result.get("deal"),
            "score": score, "rounds": len(trace.get("decisions", [])),
            "end_reason": result.get("end_reason"),
        })

        for index, turn in enumerate(trace.get("decisions", [])):
            council = turn.get("council") or {}
            if council:
                council_statuses[council.get("status", "unknown")] += 1
                if council.get("supplier_health_cost"):
                    supplier_health_cost[council["supplier_health_cost"]] += 1
                if council.get("reputation_metric"):
                    reputation_metric[council["reputation_metric"]] += 1
                for role, reason in (council.get("errors") or {}).items():
                    council_failures[f"{role}:{reason}"] += 1
            if planner is None:
                continue
            case, threads = trace.get("case"), turn.get("threads")
            research, proposal = turn.get("research"), turn.get("proposal")
            if not all((case, threads is not None, research is not None, proposal)):
                continue
            if turn.get("mode") == "deadline":
                new = planner.close_decision(case, threads, research)
            else:
                new = planner.decide(case, threads, research, _ReplayWriter())
            if _choice(new) != _choice(proposal):
                divergences.append({
                    "file": str(path), "turn": index,
                    "recorded": _choice(proposal), "current": _choice(new),
                })

    by_category_goal = {
        goal: {
            "case_count": values["case_count"],
            "completed_count": values["completed_count"],
            "deal_rate": values["deal_count"] / values["completed_count"]
                        if values["completed_count"] else None,
            "mean_score": values["score_sum"] / values["scored_count"]
                          if values["scored_count"] else None,
            "scored_count": values["scored_count"],
        }
        for goal, values in goal_results.items()
    }
    summary = {
        "trace_count": len(traces), "completed_count": completed,
        "deal_rate": deals / completed if completed else None,
        "mean_score": sum(score_values) / len(score_values) if score_values else None,
        "scored_count": len(score_values),
        "mean_decisions": sum(rounds) / len(rounds) if rounds else None,
        "failure_counts": dict(failures), "cases": cases,
        "rejection_risk": _rejection_summary(traces),
        "by_category_goal": by_category_goal,
        "council": {"decision_count": sum(council_statuses.values()),
                    "statuses": dict(council_statuses),
                    "role_failures": dict(council_failures),
                    "supplier_health_cost": dict(supplier_health_cost),
                    "reputation_metric": dict(reputation_metric)},
    }
    if replay:
        summary["decision_divergences"] = divergences
        summary["divergence_count"] = len(divergences)
        summary["replay_note"] = "Decision comparison only; supplier responses and scores are not simulated."
    return summary
