"""Regression metrics (spec §8.3).

A high decision-agreement rate is NOT evidence of model quality; it is reported alongside
policy violations and coverage, never on its own as a quality score.
"""
from __future__ import annotations

from typing import Any

import pandas as pd

from modules.evaluation.engine import CATEGORIES, RESULT_STATES

MANUAL = {"REVIEW", "HOLD", "REFER"}


def compute_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """rows: one dict per execution with keys result, category, baseline_decision,
    candidate_decision, changed, change_class, failure_types, violated_rules, error_type,
    requested_amount, scenario_faults, duration_baseline_ms, duration_candidate_ms."""
    if not rows:
        return {"total": 0}
    df = pd.DataFrame(rows)
    total = int(len(df))
    by_result = {s: int((df["result"] == s).sum()) for s in RESULT_STATES}

    coverage = []
    for cat in [c for c in CATEGORIES if c in set(df["category"])]:
        sub = df[df["category"] == cat]
        coverage.append({"category": cat, "total": int(len(sub)), **{s.lower(): int((sub["result"] == s).sum()) for s in RESULT_STATES}})

    both = df[df["baseline_decision"].notna() & df["candidate_decision"].notna()]
    agree = float((both["baseline_decision"] == both["candidate_decision"]).mean()) if len(both) else None
    matrix: dict[str, dict[str, int]] = {}
    for (b, c), n in both.groupby(["baseline_decision", "candidate_decision"]).size().items():
        matrix.setdefault(b, {})[c] = int(n)

    def count(fr, to) -> int:
        return int(((both["baseline_decision"].isin(fr)) & (both["candidate_decision"].isin(to))).sum())

    def exposure(fr, to) -> float:
        m = (both["baseline_decision"].isin(fr)) & (both["candidate_decision"].isin(to))
        return float(pd.to_numeric(both.loc[m, "requested_amount"], errors="coerce").fillna(0).sum())

    transitions = {
        "approve_to_decline": count({"APPROVE"}, {"DECLINE"}),
        "decline_to_approve": count({"DECLINE"}, {"APPROVE"}),
        "approve_to_manual": count({"APPROVE"}, MANUAL),
        "manual_to_approve": count(MANUAL, {"APPROVE"}),
        "hold_to_approve": count({"HOLD"}, {"APPROVE"}),
    }
    exposure_flips = {
        "newly_approved_amount": exposure(MANUAL | {"DECLINE"}, {"APPROVE"}),
        "no_longer_approved_amount": exposure({"APPROVE"}, MANUAL | {"DECLINE"}),
        "note": "Requested amounts on synthetic cases whose automatic-approval status changed. "
                "This is an exposure count, not a credit-loss estimate (spec §8.4).",
    }

    violations: dict[str, int] = {}
    for rules in df.loc[df["result"] == "FAIL", "violated_rules"]:
        for r in rules or []:
            violations[r] = violations.get(r, 0) + 1
    failure_types: dict[str, int] = {}
    for fts in df["failure_types"]:
        for f in fts or []:
            failure_types[f] = failure_types.get(f, 0) + 1

    dep = df[df["category"] == "DEPENDENCY_FAILURE"]
    dependency_behaviour = []
    if len(dep):
        for fault, sub in dep.groupby("scenario_faults"):
            dependency_behaviour.append({
                "fault": fault, "cases": int(len(sub)),
                "candidate_decisions": {str(k): int(v) for k, v in sub["candidate_decision"].fillna("NONE").value_counts().items()},
                "baseline_decisions": {str(k): int(v) for k, v in sub["baseline_decision"].fillna("NONE").value_counts().items()},
                "failed": int((sub["result"] == "FAIL").sum()), "errors": int((sub["result"] == "ERROR").sum()),
            })

    errors = df[df["result"] == "ERROR"]
    latency = {}
    for side in ("baseline", "candidate"):
        col = pd.to_numeric(df.get(f"duration_{side}_ms"), errors="coerce").dropna() if f"duration_{side}_ms" in df else pd.Series(dtype=float)
        latency[side] = {"p50_ms": round(float(col.quantile(0.5)), 1), "p95_ms": round(float(col.quantile(0.95)), 1)} if len(col) else None

    changes = df[df["changed"] == True]  # noqa: E712
    return {
        "total": total,
        "by_result": by_result,
        "coverage": coverage,
        "decision_agreement_rate": round(agree, 4) if agree is not None else None,
        "agreement_note": "Agreement between versions is not a measure of model quality.",
        "transition_matrix": matrix,
        "transitions": transitions,
        "exposure": exposure_flips,
        "changes": {
            "total": int(len(changes)),
            "expected": int((changes["change_class"] == "EXPECTED").sum()),
            "unexpected": int((changes["change_class"] == "UNEXPECTED").sum()),
            "prohibited": int((changes["change_class"] == "PROHIBITED").sum()),
            "with_violation": int((changes["change_class"] == "VIOLATION").sum()),
        },
        "violations_by_rule": dict(sorted(violations.items())),
        "failure_types": dict(sorted(failure_types.items())),
        "dependency_behaviour": dependency_behaviour,
        "error_rate": round(len(errors) / total, 4),
        "timeout_rate": round(float((df["error_type"] == "TIMEOUT").sum()) / total, 4),
        "inconclusive_rate": round(by_result["INCONCLUSIVE"] / total, 4),
        "errors_by_type": {str(k): int(v) for k, v in errors["error_type"].fillna("UNKNOWN").value_counts().items()},
        "latency": latency,
    }
