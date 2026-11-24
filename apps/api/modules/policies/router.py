"""Policy sets: authoring, validation, preview, four-eyes approval, versioning (FR-11 – FR-15)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from common.deps import get_or_404, row, tenant_db
from common.errors import ApiError, conflict, invalid
from common.hashing import content_hash
from db.models import PolicyRow, PolicySet, Project
from modules.audit.service import record_for
from modules.identity.auth import Principal, require
from modules.policies.dsl import PolicyError
from modules.policies.templates import TemplateError, instantiate, load_templates, summary
from modules.policies.policy_set import build_context, compile_policy_set, evaluate_policies, policy_fingerprint

router = APIRouter(prefix="/v1", tags=["policies"])


def validate_doc(content: dict[str, Any]) -> dict[str, Any]:
    try:
        cps = compile_policy_set(content)
    except PolicyError as e:
        return {"valid": False, "issues": [{"level": "error", "code": "INVALID_DEFINITION", "message": str(e), "refs": []}]}
    issues = [i.as_dict() for i in cps.issues]
    return {"valid": not cps.errors(), "issues": issues,
            "counts": {"policies": len(cps.policies), "change_controls": len(cps.change_controls),
                       "expected_changes": len(cps.expected_changes)}}


def _store_rows(db: Session, ps: PolicySet) -> None:
    db.execute(delete(PolicyRow).where(PolicyRow.policy_set_id == ps.id))
    c = ps.content
    for p in c.get("policies") or []:
        db.add(PolicyRow(organization_id=ps.organization_id, policy_set_id=ps.id, policy_id=p.get("policy_id", "?"),
                         kind="ASSERTION", expression=p, severity=p.get("severity"), enabled=bool(p.get("enabled", True))))
    for r in c.get("change_controls") or []:
        db.add(PolicyRow(organization_id=ps.organization_id, policy_set_id=ps.id, policy_id=r.get("rule_id", "?"),
                         kind="CHANGE_CONTROL", expression=r, severity=r.get("severity"), enabled=True))
    for r in c.get("expected_changes") or []:
        db.add(PolicyRow(organization_id=ps.organization_id, policy_set_id=ps.id, policy_id=r.get("change_id", "?"),
                         kind="EXPECTED_CHANGE", expression=r, severity=None, enabled=True))


class PolicySetIn(BaseModel):
    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    content: dict[str, Any]
    parent_id: uuid.UUID | None = Field(default=None, description="Previous version this revision is based on")


@router.post("/policy-sets", status_code=201)
def create_policy_set(body: PolicySetIn, p: Principal = Depends(require("policy.edit")), db: Session = Depends(tenant_db)):
    get_or_404(db, Project, body.project_id, "project")
    if body.parent_id:
        parent = get_or_404(db, PolicySet, body.parent_id, "parent policy set")
        if parent.project_id != body.project_id or parent.name != body.name:
            raise invalid("parent must be an earlier version of the same policy set")
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"ps:{body.project_id}:{body.name}"})
    version = (db.scalar(select(func.max(PolicySet.version)).where(PolicySet.project_id == body.project_id,
                                                                    PolicySet.name == body.name)) or 0) + 1
    ps = PolicySet(organization_id=p.org_id, project_id=body.project_id, name=body.name, version=version,
                   parent_id=body.parent_id, content=body.content, content_hash=content_hash(body.content),
                   validation=validate_doc(body.content), created_by=p.user_id)
    db.add(ps)
    db.flush()
    _store_rows(db, ps)
    record_for(db, p, "policy_set.create", "policy_set", ps.id,
               {"name": ps.name, "version": version, "content_hash": ps.content_hash, "valid": ps.validation["valid"]})
    db.commit()
    return row(ps)


@router.get("/policy-sets")
def list_policy_sets(project_id: uuid.UUID | None = None, p: Principal = Depends(require("project.read")),
                     db: Session = Depends(tenant_db)):
    q = select(PolicySet).order_by(PolicySet.name, PolicySet.version.desc())
    if project_id:
        q = q.where(PolicySet.project_id == project_id)
    return [row(ps, exclude={"content"}) for ps in db.scalars(q)]


@router.get("/policy-sets/{ps_id}")
def get_policy_set(ps_id: str, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    return row(get_or_404(db, PolicySet, ps_id, "policy set"))


class ContentIn(BaseModel):
    content: dict[str, Any]


@router.put("/policy-sets/{ps_id}")
def update_draft(ps_id: str, body: ContentIn, p: Principal = Depends(require("policy.edit")), db: Session = Depends(tenant_db)):
    ps = get_or_404(db, PolicySet, ps_id, "policy set")
    if ps.approval_status not in ("DRAFT", "REJECTED"):
        raise conflict(f"a {ps.approval_status} policy set cannot be edited; create a new version")
    ps.content, ps.content_hash = body.content, content_hash(body.content)
    ps.validation, ps.approval_status = validate_doc(body.content), "DRAFT"
    _store_rows(db, ps)
    record_for(db, p, "policy_set.update", "policy_set", ps.id, {"content_hash": ps.content_hash})
    db.commit()
    return row(ps)


@router.post("/policy-sets/validate")
def validate(body: ContentIn, p: Principal = Depends(require("project.read"))):
    return validate_doc(body.content)


class PreviewIn(BaseModel):
    content: dict[str, Any]
    input: dict[str, Any]
    output: dict[str, Any]
    scenario: dict[str, Any] = Field(default_factory=lambda: {"faults": {"bureau": "NONE", "fraud": "NONE", "identity": "NONE"}})


@router.post("/policy-sets/preview")
def preview(body: PreviewIn, p: Principal = Depends(require("project.read"))):
    """Policy & Test Builder preview: evaluate a draft policy set against one sample decision."""
    try:
        cps = compile_policy_set(body.content)
    except PolicyError as e:
        raise invalid(str(e)) from None
    ctx = build_context(cps, body.input, body.scenario, body.output, None)
    return {"derived": ctx["derived"], "outcomes": [o.as_dict() for o in evaluate_policies(cps, ctx)]}


class ReviewIn(BaseModel):
    note: str = Field(default="", max_length=4000)


@router.post("/policy-sets/{ps_id}/submit")
def submit(ps_id: str, body: ReviewIn, p: Principal = Depends(require("policy.edit")), db: Session = Depends(tenant_db)):
    ps = get_or_404(db, PolicySet, ps_id, "policy set")
    if ps.approval_status not in ("DRAFT", "REJECTED"):
        raise conflict(f"policy set is {ps.approval_status}")
    if not ps.validation.get("valid"):
        raise ApiError(422, "POLICY_INVALID", "fix validation errors before submitting", ps.validation.get("issues"))
    ps.approval_status, ps.submitted_by, ps.submitted_at = "PENDING_REVIEW", p.user_id, datetime.now(timezone.utc)
    ps.review_note = body.note or None
    record_for(db, p, "policy_set.submit", "policy_set", ps.id, {"version": ps.version})
    db.commit()
    return row(ps)


@router.post("/policy-sets/{ps_id}/approve")
def approve(ps_id: str, body: ReviewIn, p: Principal = Depends(require("policy.approve")), db: Session = Depends(tenant_db)):
    """FR-14: a changed policy set must be reviewed by someone other than its author/submitter."""
    ps = get_or_404(db, PolicySet, ps_id, "policy set")
    if ps.approval_status != "PENDING_REVIEW":
        raise conflict("only a policy set pending review can be approved")
    if p.user_id in (ps.created_by, ps.submitted_by):
        record_for(db, p, "policy_set.approve", "policy_set", ps.id, {"reason": "SEGREGATION_OF_DUTIES"}, outcome="DENIED")
        db.commit()
        raise ApiError(403, "SEGREGATION_OF_DUTIES", "the author or submitter of a policy set cannot approve it")
    ps.approval_status, ps.approved_by, ps.approved_at = "APPROVED", p.user_id, datetime.now(timezone.utc)
    ps.review_note = body.note or ps.review_note
    record_for(db, p, "policy_set.approve", "policy_set", ps.id, {"version": ps.version, "content_hash": ps.content_hash, "note": body.note})
    db.commit()
    return row(ps)


@router.post("/policy-sets/{ps_id}/reject")
def reject(ps_id: str, body: ReviewIn, p: Principal = Depends(require("policy.approve")), db: Session = Depends(tenant_db)):
    ps = get_or_404(db, PolicySet, ps_id, "policy set")
    if ps.approval_status != "PENDING_REVIEW":
        raise conflict("only a policy set pending review can be rejected")
    if not body.note:
        raise invalid("a rejection needs a note")
    ps.approval_status, ps.review_note = "REJECTED", body.note
    record_for(db, p, "policy_set.reject", "policy_set", ps.id, {"note": body.note})
    db.commit()
    return row(ps)


@router.get("/policy-sets/{ps_id}/diff")
def diff(ps_id: str, against: uuid.UUID, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    new, old = get_or_404(db, PolicySet, ps_id, "policy set"), get_or_404(db, PolicySet, against, "policy set")
    return policy_diff(old.content, new.content)


def policy_diff(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    def ids(doc, key, idk):
        return {x.get(idk): x for x in doc.get(key) or []}

    out: dict[str, Any] = {"parameters": {}, "policies": {}, "change_controls": {}, "expected_changes": {}}
    for k in sorted(set(old.get("parameters") or {}) | set(new.get("parameters") or {})):
        a, b = (old.get("parameters") or {}).get(k), (new.get("parameters") or {}).get(k)
        if a != b:
            out["parameters"][k] = {"from": a, "to": b}
    try:
        cold, cnew = compile_policy_set(old), compile_policy_set(new)
    except PolicyError:
        cold = cnew = None
    for key, idk in (("policies", "policy_id"), ("change_controls", "rule_id"), ("expected_changes", "change_id")):
        a, b = ids(old, key, idk), ids(new, key, idk)
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out[key][k] = "added"
            elif k not in b:
                out[key][k] = "removed"
            elif a[k] != b[k]:
                out[key][k] = "modified"
            elif key == "policies" and cold and cnew and cold.by_id(k) and cnew.by_id(k) and \
                    policy_fingerprint(cold, cold.by_id(k)) != policy_fingerprint(cnew, cnew.by_id(k)):
                out[key][k] = "behaviour changed via parameters/derived fields"
    return out


# ---- Policy templates (onboarding) ----------------------------------------------------------
@router.get("/policy-templates")
def list_templates(p: Principal = Depends(require("project.read"))):
    return [summary(t) for t in load_templates().values()]


@router.get("/policy-templates/{template_id}")
def get_template(template_id: str, p: Principal = Depends(require("project.read"))):
    t = load_templates().get(template_id)
    if t is None:
        raise ApiError(404, "NOT_FOUND", f"template '{template_id}' not found")
    return summary(t) | {"content": t["content"]}


class InstantiateIn(BaseModel):
    parameters: dict[str, Any] = Field(default_factory=dict)
    label: str | None = Field(default=None, max_length=100)


@router.post("/policy-templates/{template_id}/instantiate")
def instantiate_template(template_id: str, body: InstantiateIn, p: Principal = Depends(require("policy.edit"))):
    """Returns a policy-set document with your thresholds filled in, plus its validation. Nothing is
    saved: create a draft from the content, review it, then submit it for approval as usual."""
    if template_id not in load_templates():
        raise ApiError(404, "NOT_FOUND", f"template '{template_id}' not found")
    try:
        content = instantiate(template_id, body.parameters, body.label)
    except TemplateError as e:
        raise invalid(str(e)) from e
    return {"content": content, "validation": validate_doc(content)}
