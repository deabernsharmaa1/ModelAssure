"""Run completion: regression metrics, finding grouping, release assessment, audit."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import TestExecution, TestRun
from modules.audit.service import record
from modules.evaluation.metrics import compute_metrics


def metric_rows(db: Session, run: TestRun) -> list[dict[str, Any]]:
    rows = []
    for e in db.scalars(select(TestExecution).where(TestExecution.run_id == run.id)):
        rows.append({
            "result": e.result, "category": e.category, "baseline_decision": e.baseline_decision,
            "candidate_decision": e.candidate_decision, "changed": e.changed, "change_class": e.change_class,
            "failure_types": e.failure_types, "violated_rules": e.violated_rules, "error_type": e.error_type,
            "requested_amount": e.detail.get("requested_amount"), "scenario_faults": e.detail.get("scenario_faults") or "none",
            "duration_candidate_ms": e.duration_ms, "duration_baseline_ms": e.detail.get("duration_baseline_ms"),
        })
    return rows


def finalize_run(db: Session, run: TestRun) -> None:
    from modules.evaluation.assessment import assess_run
    from modules.findings.grouping import group_findings

    run.summary = compute_metrics(metric_rows(db, run))
    run.status = "COMPLETED"  # every case has an execution; set before assessing
    run.completed_at = datetime.now(timezone.utc)
    group_findings(db, run)
    run.assessment = assess_run(db, run)
    record(db, org_id=run.organization_id, actor_id=None, actor_label="system:worker", action="run.complete",
           resource_type="test_run", resource_id=run.id,
           details={"by_result": run.summary.get("by_result"), "assessment": (run.assessment or {}).get("outcome")})
