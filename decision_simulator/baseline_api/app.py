"""Baseline decision system: build 3.1.0 (currently in production at the fictional lender)."""
from decision_simulator.app_factory import create_app

app = create_app(["3.1.0"], "baseline")
