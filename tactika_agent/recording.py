"""Local, credential-free traces for later offline evaluation."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("taktika")


def source_fingerprint():
    digest = hashlib.sha256()
    package = Path(__file__).parent
    for source in sorted(package.glob("*.py")):
        digest.update(source.name.encode())
        digest.update(source.read_bytes())
    return digest.hexdigest()


def git_revision():
    project = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


class RunRecorder:
    def __init__(self, directory, case_id, session_id, case, initial_threads):
        root = Path(directory).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        try:
            root.chmod(0o700)
        except OSError:
            pass
        safe_case = re.sub(r"[^A-Za-z0-9._-]+", "_", str(case_id))
        safe_session = re.sub(r"[^A-Za-z0-9._-]+", "_", str(session_id or "unknown"))
        self.path = root / f"{safe_case}-{safe_session}-{uuid.uuid4().hex[:8]}.json"
        self.trace = {
            "format_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "case_id": case_id,
            "session_id": session_id,
            "git_revision": git_revision(),
            "source_fingerprint": source_fingerprint(),
            "case": self._snapshot(case),
            "initial_threads": self._snapshot(initial_threads),
            "decisions": [],
            "result": None,
            "error": None,
            "status": "in_progress",
        }
        self._save()

    @staticmethod
    def _snapshot(value):
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    def _save(self):
        if self.path is None:
            return
        temporary = self.path.with_suffix(".json.tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                os.chmod(temporary, 0o600)
                stream.write(json.dumps(self.trace, ensure_ascii=False, indent=2, default=str) + "\n")
            temporary.replace(self.path)
        except OSError:
            self.path = None
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            log.exception("Could not write local negotiation trace; continuing without recording")

    def decision(self, threads, research, decision, mode, council=None):
        entry = {
            "mode": mode,
            "threads": self._snapshot(threads),
            "research": self._snapshot(research),
            "proposal": self._snapshot(decision),
            "responses": [],
        }
        if council:
            entry["council"] = self._snapshot(council)
        self.trace["decisions"].append(entry)
        self._save()

    def response(self, response):
        if self.trace["decisions"]:
            self.trace["decisions"][-1]["responses"].append(self._snapshot(response))
            self._save()

    def finish(self, result):
        self.trace["result"] = self._snapshot(result)
        self.trace["status"] = "complete"
        self._save()

    def fail(self, error):
        self.trace["error"] = {"type": type(error).__name__, "message": str(error)}
        self.trace["status"] = "failed"
        self._save()


def _serializable_outcome(value):
    if isinstance(value, BaseException):
        return {"error": {"type": type(value).__name__, "message": str(value)}}
    if isinstance(value, dict):
        return {str(key): _serializable_outcome(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_serializable_outcome(item) for item in value]
    return value


def _result_summary(outcomes):
    if isinstance(outcomes, dict) and "score" in outcomes:
        items = [(outcomes.get("case_id", "case"), outcomes)]
    elif isinstance(outcomes, dict):
        items = list(outcomes.items())
    else:
        items = []

    scores, deals = [], 0
    for _case_id, result in items:
        if not isinstance(result, dict):
            continue
        score = result.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            scores.append(float(score) * (100 if abs(score) <= 1 else 1))
        deals += bool(result.get("deal"))
    return {
        "case_count": len(items),
        "scored_count": len(scores),
        "mean_score_percent": round(sum(scores) / len(scores), 4) if scores else None,
        "deal_count": deals,
    }


def save_results(directory, command, outcomes):
    """Persist a compact per-case result index after every CLI run."""
    root = Path(directory).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    name = datetime.now(timezone.utc).strftime("results-%Y%m%dT%H%M%SZ")
    path = root / f"{name}-{uuid.uuid4().hex[:8]}.json"
    payload = {
        "format_version": 2,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "summary": _result_summary(outcomes),
        "outcomes": _serializable_outcome(outcomes),
    }
    temporary = path.with_suffix(".json.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
        temporary.replace(path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        log.exception("Could not save run results to %s", root)
        return None
    return path
