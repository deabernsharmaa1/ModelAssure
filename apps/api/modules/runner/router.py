"""Runs and executions API (spec §10)."""
from __future__ import annotations

import json
import uuid
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.deps import get_or_404, row, tenant_db
from common.errors import conflict, not_found
from common.hashing import content_hash, sha256_hex
from common.storage import get_storage
from db.models import Review, TestExecution, TestRun
from modules.audit.service import record_for
from modules.identity.auth import Principal, require
from modules.runner.service import create_run, idempotency_lookup, idempotency_store

router = APIRouter(prefix="/v1", tags=["runs"])


class RunIn(BaseModel):
    project_id: uuid.UUID
    test_suite_id: uuid.UUID
    baseline_version_id: uuid.UUID
    candidate_version_id: uuid.UUID
    policy_set_id: uuid.UUID = Field(description="Approved policy set the candidate must satisfy")
    baseline_policy_set_id: uuid.UUID | None = Field(default=None, description="Policy set in force for the baseline (defaults to policy_set_id)")
    baseline_connection_id: uuid.UUID | None = None
    candidate_connection_id: uuid.UUID | None = None
    mode: Literal["SANDBOX"] = "SANDBOX"
    excluded_categories: list[str] = Field(default_factory=list)
    exclusion_reason: str | None = None


def run_view(run: TestRun) -> dict[str, Any]:
    d = row(run, exclude={"config_snapshot"})
    snap = run.config_snapshot
    d["config"] = {
        "suite": snap["suite"], "versions": snap["versions"], "exclusions": snap.get("exclusions"),
        "policy_sets": {k: {kk: vv for kk, vv in v.items() if kk != "content"} for k, v in snap["policy_sets"].items()},
        "connections": {k: {kk: v.get(kk) for kk in ("connection_id", "kind", "endpoint", "mapping_hash", "timeout_ms", "max_concurrency")}
                        for k, v in snap["connections"].items()},
    }
    return d


@router.post("/runs", status_code=201)
def queue_run(body: RunIn, p: Principal = Depends(require("run.create")), db: Session = Depends(tenant_db),
              idempotency_key: str | None = Header(default=None)):
    rh = content_hash(body.model_dump(mode="json"))
    hit = idempotency_lookup(db, p.org_id, idempotency_key, "POST /v1/runs", rh)
    if hit:
        db.commit()
        return JSONResponse(hit[1], status_code=hit[0], headers={"Idempotent-Replayed": "true"})
    run = create_run(db, p, project_id=body.project_id, test_suite_id=body.test_suite_id,
                     baseline_version_id=body.baseline_version_id, candidate_version_id=body.candidate_version_id,
                     policy_set_id=body.policy_set_id, baseline_policy_set_id=body.baseline_policy_set_id,
                     baseline_connection_id=body.baseline_connection_id, candidate_connection_id=body.candidate_connection_id,
                     mode=body.mode, exclusions={"categories": body.excluded_categories, "reason": body.exclusion_reason}
                     if body.excluded_categories else {})
    resp = {"run_id": str(run.id), "run_number": run.run_number, "status": run.status, "message": "Validation run accepted",
            "snapshot_hash": run.snapshot_hash}
    idempotency_store(db, p.org_id, idempotency_key, "POST /v1/runs", rh, 201, resp)
    db.commit()
    return JSONResponse(resp, status_code=201)


def _with_reviews(db: Session, views: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = [uuid.UUID(v["id"]) for v in views]
    latest: dict[str, tuple[int, str]] = {}
    for run_id, decision, n in db.execute(
            select(Review.run_id, Review.decision, func.count().over(partition_by=Review.run_id))
            .where(Review.run_id.in_(ids)).order_by(Review.run_id, Review.created_at)):
        latest[str(run_id)] = (n, decision)
    for v in views:
        n, decision = latest.get(v["id"], (0, None))
        v["review_count"], v["last_review_decision"] = n, decision
    return views


@router.get("/runs")
def list_runs(project_id: uuid.UUID | None = None, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    q = select(TestRun).order_by(TestRun.created_at.desc())
    if project_id:
        q = q.where(TestRun.project_id == project_id)
    return _with_reviews(db, [run_view(r) for r in db.scalars(q.limit(200))])


@router.get("/runs/{run_id}")
def get_run(run_id: str, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    return _with_reviews(db, [run_view(get_or_404(db, TestRun, run_id, "run"))])[0]


@router.get("/runs/{run_id}/config")
def get_run_config(run_id: str, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    run = get_or_404(db, TestRun, run_id, "run")
    snap = run.config_snapshot
    redacted = {**snap, "connections": {k: {kk: vv for kk, vv in v.items() if not kk.endswith("secret_ref")}
                                        for k, v in snap["connections"].items()}}
    return {"snapshot_hash": run.snapshot_hash, "verified": content_hash(snap) == run.snapshot_hash, "config": redacted}


@router.post("/runs/{run_id}/cancel")
def cancel(run_id: str, p: Principal = Depends(require("run.create")), db: Session = Depends(tenant_db)):
    run = get_or_404(db, TestRun, run_id, "run")
    if run.status not in ("QUEUED", "RUNNING"):
        raise conflict(f"run is {run.status}")
    run.status = "CANCELLING" if run.status == "RUNNING" else "CANCELLED"
    record_for(db, p, "run.cancel", "test_run", run.id)
    db.commit()
    return {"status": run.status}


@router.get("/runs/{run_id}/executions")
def list_executions(run_id: str, result: str | None = None, category: str | None = None, changed: bool | None = None,
                    rule: str | None = None, change_class: str | None = None,
                    limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                    p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    run = get_or_404(db, TestRun, run_id, "run")
    q = select(TestExecution).where(TestExecution.run_id == run.id)
    if result:
        q = q.where(TestExecution.result == result)
    if category:
        q = q.where(TestExecution.category == category)
    if changed is not None:
        q = q.where(TestExecution.changed == changed)
    if change_class:
        q = q.where(TestExecution.change_class == change_class)
    if rule:
        q = q.where(TestExecution.violated_rules.contains([rule]))
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    items = [row(e, exclude={"detail"}) | {"summary": _exec_summary(e)} for e in
             db.scalars(q.order_by(TestExecution.case_key).limit(limit).offset(offset))]
    return {"total": total, "items": items}


def _exec_summary(e: TestExecution) -> dict[str, Any]:
    d = e.detail
    return {"violations": d.get("violations", []), "inconclusive_reasons": d.get("inconclusive_reasons", []),
            "explained_by": d.get("explained_by", []), "error": d.get("error"), "change_flags": d.get("change_flags", [])}


@router.get("/runs/{run_id}/executions/{execution_id}")
def get_execution(run_id: str, execution_id: str, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    """Full reproducible evidence for one case. The stored evidence is re-hashed on every read."""
    e = get_or_404(db, TestExecution, execution_id, "execution")
    if str(e.run_id) != run_id:
        raise not_found("execution")
    try:
        raw = get_storage().get(e.evidence_ref)
    except (FileNotFoundError, KeyError):
        # Raw payloads were removed under the retention policy; the hash and outcome remain.
        return {**row(e), "evidence": None,
                "evidence_integrity": {"sha256": e.evidence_hash, "verified": False, "purged": True}}
    except Exception as exc:  # e.g. S3 NoSuchKey
        if "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc):
            return {**row(e), "evidence": None,
                    "evidence_integrity": {"sha256": e.evidence_hash, "verified": False, "purged": True}}
        raise
    intact = sha256_hex(raw) == e.evidence_hash
    return {**row(e), "evidence": json.loads(raw), "evidence_integrity": {"sha256": e.evidence_hash, "verified": intact, "purged": False}}
