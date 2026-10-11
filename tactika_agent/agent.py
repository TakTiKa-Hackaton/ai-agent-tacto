"""Top-level arena agent that coordinates research, offer planning, and email drafting."""

import logging
import os
from pathlib import Path

from .council import SpecialistCouncil
from .email_writer import EmailWriter
from .research import ProcurementResearch, ResearchStore
from .strategy import OfferPlanner

log = logging.getLogger("taktika")


def _research_root(configured, project_root):
    """Resolve research data relative to this project, independent of launch CWD."""
    project_root = Path(project_root).expanduser().resolve()
    value = (configured or "data").strip() or "data"
    root = Path(value).expanduser()
    if not root.is_absolute():
        root = (project_root / root).resolve()
    if root.is_dir():
        return root

    bundled = project_root / "data"
    if bundled.is_dir():
        if value != "data":
            log.warning("ARENA_DATA path %s was not found; using bundled research data", root)
        return bundled
    return None


class TaktikaAgent:
    def __init__(self, package=None, writer=None, council=None):
        if package is None:
            project_root = Path(__file__).resolve().parent.parent
            root = _research_root(os.getenv("ARENA_DATA"), project_root)
            release = os.getenv("ARENA_RELEASE", "").strip()
            if release:
                release_path = Path(release).expanduser()
                if not release_path.is_absolute():
                    release_path = (project_root / release_path).resolve()
                release = str(release_path)
            package = ResearchStore(root, release or None) if root else None
        self.researcher = ProcurementResearch(package)
        self.planner = OfferPlanner(self.researcher)
        self.writer = writer or EmailWriter()
        self.council = council or SpecialistCouncil()
        self.last_research = None
        self.last_council = None

    def decide(self, case, threads):
        evidence = self.researcher.collect(case)
        self.last_research = evidence
        self.last_council = None
        if self.council.enabled:
            decision = self.council.decide(case, threads, evidence, self.planner)
            self.last_council = self.council.last_audit
            if self.last_council is not None:
                self.last_council["api_cost_usd"] = round(
                    sum(call.get("api_cost_usd", 0)
                        for call in self.last_council.get("requests", [])), 8
                )
            return decision
        return self.planner.decide(case, threads, evidence, self.writer)

    def close_decision(self, case, threads):
        """Choose a BATNA-safe outcome without drafting email at a deadline."""
        evidence = self.researcher.collect(case)
        self.last_research = evidence
        self.last_council = None
        return self.planner.close_decision(case, threads, evidence)
