"""Findings investigation, retest and human release review (Modules F and G, spec §13)."""
from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.deps import get_or_404, row, tenant_db
from common.errors import ApiError, conflict, invalid
from common.hashing import content_hash
from db.models import (
    Connection, Finding, FindingExecution, Membership, Review, SystemVersion, TestExecution, TestRun, User,
)
from modules.audit.service import record_for
from modules.evaluation.assessment import refresh_assessment
from modules.identity.auth import Principal, require
from modules.runner.service import create_run, idempotency_lookup, idempotency_store

router = APIRouter(prefix="/v1", tags=["findings & release review"])

STATUSES = ("OPEN", "INVESTIGATING", "FIX_PROPOSED", "READY_FOR_RETEST", "RESOLVED", "ACCEPTED_RISK", "CLOSED")
TRANSITIONS: dict[str, set[str]] = {
    "OPEN": {"INVESTIGATING", "FIX_PROPOSED", "ACCEPTED_RISK", "CLOSED"},
    "INVESTIGATING": {"OPEN", "FIX_PROPOSED", "ACCEPTED_RISK", "CLOSED"},
    "FIX_PROPOSED": {"INVESTIGATING", "READY_FOR_RETEST", "OPEN"},
    "READY_FOR_RETEST": {"FIX_PROPOSED", "INVESTIGATING"},
    "RESOLVED": {"CLOSED", "OPEN"},
    "ACCEPTED_RISK": {"OPEN", "CLOSED"},
    "CLOSED": {"OPEN"},
}


def finding_view(db: Session, f: Finding) -> dict[str, Any]:
    d = row(f)
    d["key"] = f"F-{f.finding_number}"
    if f.owner_id:
        u = db.get(User, f.owner_id)
        d["owner"] = {"id": str(u.id), "name": u.name, "email": u.email} if u else None
    run = db.get(TestRun, f.run_id)
    d["run_number"] = run.run_number if run else None
    return d


@router.get("/findings")
def search_findings(project_id: uuid.UUID | None = None, run_id: uuid.UUID | None = None, status: str | None = None,
                    severity: str | None = None, type: str | None = None,
                    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                    p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    q = select(Finding)
    for col, val in ((Finding.project_id, project_id), (Finding.run_id, run_id), (Finding.status, status),
                     (Finding.severity, severity), (Finding.type, type)):
        if val is not None:
            q = q.where(col == val)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    items = db.scalars(q.order_by(Finding.finding_number).limit(limit).offset(offset))
    return {"total": total, "items": [finding_view(db, f) for f in items]}


@router.get("/findings/{finding_id}")
def get_finding(finding_id: str, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    f = get_or_404(db, Finding, finding_id, "finding")
    d = finding_view(db, f)
    ex = db.scalars(select(TestExecution).join(FindingExecution, FindingExecution.execution_id == TestExecution.id)
                    .where(FindingExecution.finding_id == f.id).order_by(TestExecution.case_key).limit(50))
    d["executions"] = [{**row(e, exclude={"detail"}), "violations": e.detail.get("violations", []),
                        "inconclusive_reasons": e.detail.get("inconclusive_reasons", []), "error": e.detail.get("error"),
                        "explained_by": e.detail.get("explained_by", [])} for e in ex]
    return d


class RiskAcceptanceIn(BaseModel):
    owner_id: uuid.UUID = Field(description="Named owner of the accepted risk")
    rationale: str = Field(min_length=20, max_length=4000)
    expires_on: date | None = None


class ClosureIn(BaseModel):
    reason: Literal["VERIFIED_FIXED", "RISK_ACCEPTED", "FALSE_POSITIVE", "DUPLICATE", "NOT_APPLICABLE"]
    rationale: str = Field(min_length=10, max_length=4000)


class FindingPatch(BaseModel):
    status: Literal["OPEN", "INVESTIGATING", "FIX_PROPOSED", "READY_FOR_RETEST", "ACCEPTED_RISK", "CLOSED"] | None = None
    owner_id: uuid.UUID | None = None
    suspected_cause: str | None = Field(default=None, max_length=4000)
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"] | None = None
    severity_rationale: str | None = None
    comment: str | None = Field(default=None, max_length=4000)
    risk_acceptance: RiskAcceptanceIn | None = None
    closure: ClosureIn | None = None


def _member(db: Session, org_id, user_id) -> User:
    if not db.scalar(select(Membership.id).where(Membership.organization_id == org_id, Membership.user_id == user_id).limit(1)):
        raise invalid("user is not a member of this organisation")
    return db.get(User, user_id)


@router.patch("/findings/{finding_id}")
def update_finding(finding_id: str, body: FindingPatch, p: Principal = Depends(require("finding.update")),
                   db: Session = Depends(tenant_db)):
    f = get_or_404(db, Finding, finding_id, "finding")
    now = datetime.now(timezone.utc)
    events: list[dict[str, Any]] = []

    def denied(perm: str):
        from modules.audit.service import record_denied

        record_denied(p, perm, f"/v1/findings/{finding_id}")
        raise ApiError(403, "FORBIDDEN", f"permission '{perm}' required")

    if body.owner_id is not None:
        u = _member(db, p.org_id, body.owner_id)
        f.owner_id = u.id
        events.append({"event": "owner_assigned", "owner": u.email})
    if body.suspected_cause is not None:
        f.suspected_cause = body.suspected_cause
        events.append({"event": "suspected_cause_updated"})
    if body.severity is not None and body.severity != f.severity:
        if not p.can("finding.severity"):
            denied("finding.severity")
        if not body.severity_rationale:
            raise invalid("changing severity requires severity_rationale")
        events.append({"event": "severity_changed", "from": f.severity, "to": body.severity, "rationale": body.severity_rationale})
        f.severity = body.severity
    if body.comment:
        events.append({"event": "comment", "text": body.comment})
    if body.status is not None and body.status != f.status:
        if body.status not in TRANSITIONS[f.status]:
            raise conflict(f"cannot move a finding from {f.status} to {body.status}"
                           + (" (RESOLVED is set only by a passing retest)" if body.status == "RESOLVED" else ""))
        if body.status == "ACCEPTED_RISK":
            if not p.can("finding.accept_risk"):
                denied("finding.accept_risk")
            if body.risk_acceptance is None:
                raise invalid("risk acceptance requires a named owner and rationale")
            owner = _member(db, p.org_id, body.risk_acceptance.owner_id)
            f.risk_acceptance = {"owner_id": str(owner.id), "owner_email": owner.email, "rationale": body.risk_acceptance.rationale,
                                 "expires_on": body.risk_acceptance.expires_on.isoformat() if body.risk_acceptance.expires_on else None,
                                 "authorised_by": p.email, "authorised_at": now.isoformat(), "mfa": p.mfa}
        if body.status == "CLOSED":
            if not p.can("finding.close"):
                denied("finding.close")
            if body.closure is None:
                raise invalid("closing a finding requires a closure reason and rationale")
            if f.status in ("OPEN", "INVESTIGATING") and body.closure.reason not in ("FALSE_POSITIVE", "DUPLICATE", "NOT_APPLICABLE"):
                raise invalid("an unresolved finding can only be closed as FALSE_POSITIVE, DUPLICATE or NOT_APPLICABLE")
            f.closure = {**body.closure.model_dump(), "closed_by": p.email, "closed_at": now.isoformat()}
        if body.status == "OPEN" and f.status == "ACCEPTED_RISK":
            f.risk_acceptance = {**(f.risk_acceptance or {}), "revoked_by": p.email, "revoked_at": now.isoformat()}
        events.append({"event": "status_changed", "from": f.status, "status": body.status})
        f.status = body.status
    if not events:
        raise invalid("nothing to update")
    f.history = [*f.history, *[{"at": now.isoformat(), "by": p.email, **e} for e in events]]
    f.updated_at = now
    record_for(db, p, "finding.update", "finding", f.id, {"finding": f"F-{f.finding_number}", "events": events})
    run = db.get(TestRun, f.run_id)
    refresh_assessment(db, run)
    db.commit()
    return finding_view(db, f)


# ---- retest --------------------------------------------------------------------------------------
class RetestIn(BaseModel):
    candidate_version_id: uuid.UUID | None = Field(default=None, description="Fixed build to test (defaults to the run's candidate)")
    scope: Literal["AFFECTED", "FULL"] = "AFFECTED"
    finding_ids: list[uuid.UUID] | None = Field(default=None, description="Defaults to the run's READY_FOR_RETEST findings")


@router.post("/runs/{run_id}/retest", status_code=201)
def retest(run_id: str, body: RetestIn, p: Principal = Depends(require("run.create")), db: Session = Depends(tenant_db),
           idempotency_key: str | None = Header(default=None)):
    rh = content_hash({"run": run_id, **body.model_dump(mode="json")})
    hit = idempotency_lookup(db, p.org_id, idempotency_key, f"POST /v1/runs/{run_id}/retest", rh)
    if hit:
        db.commit()
        return JSONResponse(hit[1], status_code=hit[0])
    orig = get_or_404(db, TestRun, run_id, "run")
    if orig.status != "COMPLETED":
        raise conflict("only a completed run can be retested")
    q = select(Finding).where(Finding.run_id == orig.id)
    if body.finding_ids:
        findings = list(db.scalars(q.where(Finding.id.in_(body.finding_ids))))
        if len(findings) != len(set(body.finding_ids)):
            raise invalid("some findings do not belong to this run")
    else:
        findings = list(db.scalars(q.where(Finding.status == "READY_FOR_RETEST")))
    if not findings:
        raise conflict("no findings are ready for retest; move fixed findings to READY_FOR_RETEST first")
    not_ready = [f"F-{f.finding_number}" for f in findings if f.status != "READY_FOR_RETEST"]
    if not_ready:
        raise conflict(f"findings not ready for retest: {', '.join(not_ready)}")
    case_filter = None
    if body.scope == "AFFECTED":
        case_filter = sorted({str(c) for c in db.scalars(
            select(TestExecution.case_id).join(FindingExecution, FindingExecution.execution_id == TestExecution.id)
            .where(FindingExecution.finding_id.in_([f.id for f in findings])))})
    run = create_run(db, p, project_id=orig.project_id, test_suite_id=orig.suite_id,
                     baseline_version_id=orig.baseline_version_id,
                     candidate_version_id=body.candidate_version_id or orig.candidate_version_id,
                     policy_set_id=orig.policy_set_id, baseline_policy_set_id=orig.baseline_policy_set_id,
                     scope="RETEST" if body.scope == "AFFECTED" else "FULL", parent_run_id=orig.id,
                     case_filter=case_filter, retest_finding_ids=[str(f.id) for f in findings],
                     exclusions=orig.config_snapshot.get("exclusions"))
    for f in findings:
        f.history = [*f.history, {"at": datetime.now(timezone.utc).isoformat(), "by": p.email, "event": "retest_queued",
                                  "run_number": run.run_number}]
    resp = {"run_id": str(run.id), "run_number": run.run_number, "status": run.status, "scope": run.scope,
            "parent_run_id": str(orig.id), "findings": [f"F-{f.finding_number}" for f in findings],
            "cases": len(case_filter) if case_filter else run.config_snapshot["suite"]["case_count"]}
    idempotency_store(db, p.org_id, idempotency_key, f"POST /v1/runs/{run_id}/retest", rh, 201, resp)
    db.commit()
    return JSONResponse(resp, status_code=201)


# ---- assessment & human review -----------------------------------------------------------------------
@router.get("/runs/{run_id}/assessment")
def get_assessment(run_id: str, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    run = get_or_404(db, TestRun, run_id, "run")
    a = refresh_assessment(db, run)
    db.commit()
    return a


class ReviewIn(BaseModel):
    decision: Literal["APPROVE_RELEASE", "REJECT_RELEASE", "REQUEST_REMEDIATION"]
    rationale: str = Field(min_length=10, max_length=8000)


def _conflicts_of_interest(db: Session, run: TestRun, p: Principal) -> list[str]:
    """Spec §12.2: people who develop a system cannot approve its release."""
    from modules.gate.keys import accountable  # a pipeline key's creator answers for what the key did

    reasons = []
    cand = db.get(SystemVersion, run.candidate_version_id)
    if cand and p.user_id in accountable(db, cand.registered_by):
        reasons.append("registered the candidate version")
    conn_id = run.config_snapshot["connections"]["candidate"]["connection_id"]
    conn = db.get(Connection, uuid.UUID(conn_id))
    if conn and p.user_id in accountable(db, conn.created_by):
        reasons.append("configured the candidate connection")
    if p.user_id in accountable(db, run.created_by):
        reasons.append("initiated the test run")
    return reasons


@router.post("/runs/{run_id}/reviews", status_code=201)
def record_review(run_id: str, body: ReviewIn, p: Principal = Depends(require("release.review")), db: Session = Depends(tenant_db)):
    run = get_or_404(db, TestRun, run_id, "run")
    coi = _conflicts_of_interest(db, run, p)
    if coi:
        record_for(db, p, "release.review", "test_run", run.id, {"reason": "SEGREGATION_OF_DUTIES", "conflicts": coi}, outcome="DENIED")
        db.commit()
        raise ApiError(403, "SEGREGATION_OF_DUTIES", f"you cannot review this release because you {', '.join(coi)}")
    if run.status != "COMPLETED":
        raise conflict("the run has not completed")
    assessment = refresh_assessment(db, run)
    if body.decision == "APPROVE_RELEASE" and assessment.get("outcome") != "PASS":
        raise conflict(f"release cannot be approved while the assessment is {assessment.get('outcome')}: "
                       "remediate and retest, or have the Risk Manager document risk acceptance for each blocking finding",
                       {"fail_reasons": assessment.get("fail_reasons"), "inconclusive_reasons": assessment.get("inconclusive_reasons")})
    rv = Review(organization_id=p.org_id, run_id=run.id, reviewer_id=p.user_id, decision=body.decision,
                rationale=body.rationale, assessment_snapshot=assessment)
    db.add(rv)
    db.flush()
    record_for(db, p, "release.review", "test_run", run.id, {"decision": body.decision, "assessment": assessment.get("outcome"),
                                                              "candidate": run.config_snapshot["versions"]["candidate"]["version"]})
    db.commit()
    return {**row(rv), "note": "Recorded. ModelAssure does not deploy; the institution performs any release through its own change process."}


@router.get("/runs/{run_id}/reviews")
def list_reviews(run_id: str, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    run = get_or_404(db, TestRun, run_id, "run")
    out = []
    for rv in db.scalars(select(Review).where(Review.run_id == run.id).order_by(Review.created_at)):
        u = db.get(User, rv.reviewer_id)
        out.append({**row(rv), "reviewer": {"name": u.name, "email": u.email} if u else None})
    return out
