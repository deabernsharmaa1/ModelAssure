"""Candidate decision system: build 3.2.0 (release under test, with seeded defects) and
build 3.2.1 (the engineering fix, used to demonstrate retesting)."""
from decision_simulator.app_factory import create_app

app = create_app(["3.2.0", "3.2.1"], "candidate")
