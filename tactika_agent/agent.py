"""Top-level arena agent that coordinates research, offer planning, and email drafting."""

import os
from pathlib import Path

from .email_writer import EmailWriter
from .research import ProcurementResearch, ResearchStore
from .strategy import OfferPlanner


class TaktikaAgent:
    def __init__(self, package=None, writer=None):
        if package is None:
            root = os.getenv("ARENA_DATA", "data")
            package = ResearchStore(root, os.getenv("ARENA_RELEASE")) if root and Path(root).exists() else None
        self.researcher = ProcurementResearch(package)
        self.planner = OfferPlanner(self.researcher)
        self.writer = writer or EmailWriter()

    def decide(self, case, threads):
        evidence = self.researcher.collect(case)
        return self.planner.decide(case, threads, evidence, self.writer)
