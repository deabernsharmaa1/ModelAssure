"""Creating runs: validation, configuration locking (snapshot) and queueing."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from common.deps import get_or_404
from common.errors import ApiError, conflict, invalid
from common.hashing import content_hash
from db.models import Connection, IdempotencyKey, PolicySet, Project, SystemVersion, TestRun, TestSuite
from modules.audit.service import record_for
from modules.connectors.router import connection_snapshot
from modules.policies.dsl import PolicyError
from modules.policies.policy_set import compile_policy_set
from modules.runner.queue import enqueue

PLATFORM_VERSION = "modelassure-1.0.0"


def _pick_connection(db: Session, version: SystemVersion, explicit: uuid.UUID | None) -> Connection:
    if explicit:
        c = get_or_404(db, Connection, explicit, "connection")
        if c.system_version_id != version.id:
            raise invalid(f"connection {explicit} does not belong to version {version.version}")
    else:
        c = db.scalars(select(Connection).where(Connection.system_version_id == version.id, Connection.status == "VERIFIED")
                       .order_by(Connection.verified_at.desc()).limit(1)).first()
        if c is None:
            raise conflict(f"version {version.version} has no verified connection; verify a connection first")
    if c.status != "VERIFIED":
        raise conflict(f"connection {c.id} is not verified")
    if c.environment != "SANDBOX":
        raise invalid("only sandbox connections can be tested")
    return c


def _policy_snapshot(ps: PolicySet) -> dict[str, Any]:
    try:
        cps = compile_policy_set(ps.content)
    except PolicyError as e:
        raise invalid(f"policy set {ps.name} v{ps.version} is invalid: {e}") from None
    if cps.errors():
        raise ApiError(422, "POLICY_INVALID", f"policy set {ps.name} v{ps.version} has validation errors",
                       [i.as_dict() for i in cps.errors()])
    return {"id": str(ps.id), "name": ps.name, "version": ps.version, "content_hash": ps.content_hash,
            "approval_status": ps.approval_status, "approved_by": str(ps.approved_by) if ps.approved_by else None,
            "label": ps.content.get("label"), "content": ps.content}


def create_run(db: Session, p, *, project_id, test_suite_id, baseline_version_id, candidate_version_id, policy_set_id,
               baseline_policy_set_id=None, baseline_connection_id=None, candidate_connection_id=None,
               mode: str = "SANDBOX", exclusions: dict[str, Any] | None = None, scope: str = "FULL",
               parent_run_id=None, case_filter: list[str] | None = None, retest_finding_ids: list[str] | None = None) -> TestRun:
    if mode != "SANDBOX":
        raise invalid("ModelAssure only executes against approved sandbox environments (mode must be SANDBOX)")
    project = get_or_404(db, Project, project_id, "project")
    suite = get_or_404(db, TestSuite, test_suite_id, "test suite")
    if suite.project_id != project.id:
        raise invalid("test suite belongs to another project")
    bv = get_or_404(db, SystemVersion, baseline_version_id, "baseline version")
    cv = get_or_404(db, SystemVersion, candidate_version_id, "candidate version")
    if bv.system_id != project.system_id or cv.system_id != project.system_id:
        raise invalid("versions must belong to the project's system")
    if bv.id == cv.id:
        raise invalid("baseline and candidate must be different versions")
    cps = get_or_404(db, PolicySet, policy_set_id, "policy set")
    bps = get_or_404(db, PolicySet, baseline_policy_set_id or policy_set_id, "baseline policy set")
    for ps in (cps, bps):
        if ps.project_id != project.id:
            raise invalid("policy sets must belong to the project")
    bconn = _pick_connection(db, bv, baseline_connection_id)
    cconn = _pick_connection(db, cv, candidate_connection_id)
    exclusions = exclusions or {}
    if exclusions.get("categories") and not exclusions.get("reason"):
        raise invalid("excluding categories requires a documented reason")

    snapshot = {
        "platform_version": PLATFORM_VERSION,
        "project": {"id": str(project.id), "name": project.name, "risk_classification": project.risk_classification},
        "suite": {"id": str(suite.id), "name": suite.name, "version": suite.version, "content_hash": suite.content_hash,
                  "case_count": suite.case_count, "category_counts": suite.category_counts},
        "versions": {"baseline": {"id": str(bv.id), "version": bv.version, "checksum": bv.checksum},
                     "candidate": {"id": str(cv.id), "version": cv.version, "checksum": cv.checksum}},
        "policy_sets": {"candidate": _policy_snapshot(cps), "baseline": _policy_snapshot(bps)},
        "connections": {"baseline": connection_snapshot(bconn, bv), "candidate": connection_snapshot(cconn, cv)},
        "exclusions": exclusions, "scope": scope,
    }
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"run:{project.id}"})
    number = (db.scalar(select(func.max(TestRun.run_number)).where(TestRun.project_id == project.id)) or 1000) + 1
    run = TestRun(
        id=uuid.uuid4(), organization_id=p.org_id, project_id=project.id, run_number=number, suite_id=suite.id,
        baseline_version_id=bv.id, candidate_version_id=cv.id, policy_set_id=cps.id, baseline_policy_set_id=bps.id,
        mode=mode, scope=scope, parent_run_id=parent_run_id, case_filter=case_filter,
        retest_finding_ids=retest_finding_ids, config_snapshot=snapshot, snapshot_hash=content_hash(snapshot),
        progress={"completed": 0, "total": len(case_filter) if case_filter else suite.case_count, "percent": 0.0},
        created_by=p.user_id,
    )
    db.add(run)
    db.flush()
    enqueue(db, p.org_id, "execute_run", {"run_id": str(run.id)})
    record_for(db, p, "run.create", "test_run", run.id, {
        "run_number": number, "scope": scope, "snapshot_hash": run.snapshot_hash, "baseline": bv.version,
        "candidate": cv.version, "policy_set": f"{cps.name} v{cps.version} ({cps.approval_status})",
        "suite": f"{suite.name} v{suite.version}", "parent_run_id": str(parent_run_id) if parent_run_id else None})
    return run


# ---- idempotency ---------------------------------------------------------------------------------
def idempotency_lookup(db: Session, org_id, key: str | None, endpoint: str, request_hash: str):
    if not key:
        return None
    if len(key) > 200:
        raise invalid("Idempotency-Key too long")
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"idem:{org_id}:{endpoint}:{key}"})
    hit = db.get(IdempotencyKey, (org_id, key, endpoint))
    if hit is None:
        return None
    if hit.request_hash != request_hash:
        raise ApiError(422, "IDEMPOTENCY_KEY_REUSED", "this Idempotency-Key was already used with a different request")
    return hit.status_code, hit.response


def idempotency_store(db: Session, org_id, key: str | None, endpoint: str, request_hash: str, status: int, response: dict) -> None:
    if key:
        db.add(IdempotencyKey(organization_id=org_id, key=key, endpoint=endpoint, request_hash=request_hash,
                              status_code=status, response=response))
