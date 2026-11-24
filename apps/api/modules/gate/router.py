"""Release gate for CI/CD pipelines, and the admin endpoints for pipeline keys.

A pipeline calls `POST /v1/gate/runs` with the new build's version and sandbox endpoint. The gate
registers the version (or reuses it), clones the project's last verified candidate connection
with the new endpoint and verifies it, then queues a full run with the same suite and policy sets
as the project's latest completed full run, against the project's baseline. The pipeline polls
`GET /v1/gate/runs/{id}?wait=30` and fails the build on FAIL or INCONCLUSIVE.

A gate PASS means the defined tests passed. It is not a release authorisation: approval stays with
an authorised human reviewer, who cannot be the person who created the pipeline key.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.deps import get_or_404, tenant_db
from common.errors import ApiError, conflict, invalid
from common.hashing import content_hash
from db.models import ApiKey, Connection, Finding, Project, SystemVersion, TestRun
from modules.audit.service import record_for
from modules.connectors.router import verify_connection
from modules.findings.grouping import open_for_build
from modules.gate.keys import create_key, public, revoke_key
from modules.identity.auth import Principal, require
from modules.runner.service import create_run, idempotency_lookup, idempotency_store
from settings import get_settings

router = APIRouter(prefix="/v1", tags=["release gate"])

EXIT_CODES = {"PASS": 0, "FAIL": 1, "INCONCLUSIVE": 2}
STATEMENT = ("A gate PASS means the defined tests passed under the stated configuration. It is not a release "
             "authorisation; an authorised reviewer records the release decision in ModelAssure.")
FINISHED = ("COMPLETED", "FAILED", "CANCELLED")


def _scope(p: Principal, project_id) -> None:
    if p.scope_project_id is not None and str(p.scope_project_id) != str(project_id):
        raise ApiError(403, "FORBIDDEN", "this API key is scoped to another project")


class GateIn(BaseModel):
    project_id: uuid.UUID
    candidate_version: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9._+-]+$")
    candidate_endpoint: str | None = Field(default=None, description="Sandbox endpoint of the new build. Needed unless "
                                           "the version already has a verified connection.")
    expected_model_version: str | None = Field(default=None, max_length=50,
                                               description="Version the endpoint must report; defaults to candidate_version")
    description: str | None = Field(default=None, max_length=500)
    source: dict[str, str] = Field(default_factory=dict, description="Pipeline metadata, e.g. commit, ref, pipeline_url")
    test_suite_id: uuid.UUID | None = None
    policy_set_id: uuid.UUID | None = None
    baseline_policy_set_id: uuid.UUID | None = None


def _template_run(db: Session, project: Project) -> TestRun:
    run = db.scalars(select(TestRun).where(TestRun.project_id == project.id, TestRun.status == "COMPLETED",
                                           TestRun.scope == "FULL").order_by(TestRun.run_number.desc()).limit(1)).first()
    if run is None:
        raise conflict("the project has no completed full run yet. Run the release suite once from ModelAssure; "
                       "the gate reuses its suite, policy sets and connection settings")
    return run


def _version(db: Session, p: Principal, project: Project, body: GateIn) -> SystemVersion:
    expected = body.expected_model_version or body.candidate_version
    v = db.scalars(select(SystemVersion).where(SystemVersion.system_id == project.system_id,
                                               SystemVersion.version == body.candidate_version)).first()
    if v is not None:
        if v.expected_model_version and v.expected_model_version != expected:
            raise conflict(f"version {v.version} is registered with expected model version {v.expected_model_version!r}, "
                           f"not {expected!r}. Versions are immutable; use a new version number")
        return v
    description = body.description or f"Registered by release gate ({p.name})"
    v = SystemVersion(organization_id=p.org_id, system_id=project.system_id, version=body.candidate_version,
                      expected_model_version=expected, description=description, registered_by=p.user_id,
                      checksum=content_hash({"system_id": str(project.system_id), "version": body.candidate_version,
                                             "expected_model_version": expected, "artifact_digest": None,
                                             "description": description}))
    db.add(v)
    db.flush()
    record_for(db, p, "system_version.register", "system_version", v.id,
               {"version": v.version, "via": "release_gate", "source": body.source})
    return v


def _connection(db: Session, p: Principal, version: SystemVersion, template: TestRun, endpoint: str | None) -> Connection:
    existing = db.scalars(select(Connection).where(Connection.system_version_id == version.id, Connection.status == "VERIFIED")
                          .order_by(Connection.verified_at.desc()).limit(1)).first()
    if existing is not None and (endpoint is None or existing.endpoint == endpoint):
        return existing
    if endpoint is None:
        raise invalid(f"version {version.version} has no verified connection; pass candidate_endpoint")
    t = db.get(Connection, uuid.UUID(template.config_snapshot["connections"]["candidate"]["connection_id"]))
    if t is None or t.kind != "REST":
        raise conflict("the template run's candidate connection is not a REST connection")
    from modules.connectors.egress import EgressError, check_url
    from db.models import Organization

    org = db.get(Organization, p.org_id)
    try:
        check_url(endpoint, org.approved_destinations if org else [])
    except EgressError as e:
        raise invalid(f"endpoint not permitted: {e}") from None
    c = Connection(organization_id=p.org_id, system_version_id=version.id, kind="REST", endpoint=endpoint,
                   environment="SANDBOX", auth_header=t.auth_header, secret_ref=t.secret_ref, mapping=t.mapping,
                   mapping_hash=t.mapping_hash, timeout_ms=t.timeout_ms, max_concurrency=t.max_concurrency, max_rps=t.max_rps,
                   dependency_control_url=t.dependency_control_url, dependency_secret_ref=t.dependency_secret_ref,
                   created_by=p.user_id)
    db.add(c)
    db.flush()
    record_for(db, p, "connection.create", "connection", c.id,
               {"system_version_id": str(version.id), "endpoint": endpoint, "cloned_from": str(t.id), "via": "release_gate"})
    db.commit()
    result = verify_connection(str(c.id), p, db)  # commits; failure is recorded on the connection
    if result["status"] != "VERIFIED":
        raise ApiError(422, "CONNECTION_NOT_VERIFIED", "the new build's endpoint failed verification", result["checks"])
    return c


@router.post("/gate/runs", status_code=202)
def start_gate(body: GateIn, p: Principal = Depends(require("gate.run")), db: Session = Depends(tenant_db),
               idempotency_key: str | None = Header(default=None)):
    _scope(p, body.project_id)
    endpoint_key = "POST /v1/gate/runs"
    req_hash = content_hash(body.model_dump(mode="json"))
    hit = idempotency_lookup(db, p.org_id, idempotency_key, endpoint_key, req_hash)
    if hit:
        return hit[1]
    project = get_or_404(db, Project, body.project_id, "project")
    template = _template_run(db, project)
    if not project.baseline_version_id:
        raise conflict("the project has no baseline version")
    version = _version(db, p, project, body)
    if version.id == project.baseline_version_id:
        raise invalid("the candidate version is the project's baseline")
    db.commit()
    conn = _connection(db, p, version, template, body.candidate_endpoint)
    hit = idempotency_lookup(db, p.org_id, idempotency_key, endpoint_key, req_hash)  # re-check after the commits
    if hit:
        return hit[1]
    run = create_run(db, p, project_id=project.id, test_suite_id=body.test_suite_id or template.suite_id,
                     baseline_version_id=project.baseline_version_id, candidate_version_id=version.id,
                     policy_set_id=body.policy_set_id or template.policy_set_id,
                     baseline_policy_set_id=body.baseline_policy_set_id or template.baseline_policy_set_id,
                     candidate_connection_id=conn.id)
    record_for(db, p, "gate.start", "test_run", run.id,
               {"run_id": str(run.id), "candidate": version.version, "template_run": template.run_number,
                "source": body.source, "api_key_id": str(p.api_key_id) if p.api_key_id else None})
    resp = {"run_id": str(run.id), "run_number": run.run_number, "status": run.status,
            "candidate_version": version.version, "baseline_version": template.config_snapshot["versions"]["baseline"]["version"]
            if template.baseline_version_id == project.baseline_version_id else None,
            "status_url": f"/v1/gate/runs/{run.id}", "statement": STATEMENT}
    idempotency_store(db, p.org_id, idempotency_key, endpoint_key, req_hash, 202, resp)
    db.commit()
    return resp


def _result(db: Session, run: TestRun) -> dict[str, Any]:
    a = run.assessment or {}
    outcome = a.get("outcome") if run.status == "COMPLETED" else None
    if run.status in ("FAILED", "CANCELLED"):
        exit_code = 3
    else:
        exit_code = EXIT_CODES.get(outcome) if outcome else None
    cand = run.config_snapshot["versions"]["candidate"]["version"]
    blocking = [f"F-{f.finding_number} {f.severity} {f.title}" for f in
                db.scalars(select(Finding).where(Finding.run_id == run.id).order_by(Finding.finding_number))
                if open_for_build(f, cand) and f.severity in ("CRITICAL", "HIGH")]
    return {"run_id": str(run.id), "run_number": run.run_number, "status": run.status, "finished": run.status in FINISHED,
            "outcome": outcome, "exit_code": exit_code, "progress": run.progress,
            "baseline_version": run.config_snapshot["versions"]["baseline"]["version"], "candidate_version": cand,
            "results": (run.summary or {}).get("by_result"),
            "fail_reasons": a.get("fail_reasons", []), "inconclusive_reasons": a.get("inconclusive_reasons", []),
            "flags": a.get("flags", []), "blocking_findings": blocking,
            "release_review_url": f"{get_settings().web_url.rstrip('/')}/projects/{run.project_id}/release?run={run.id}",
            "statement": STATEMENT}


@router.get("/gate/runs/{run_id}")
def gate_status(run_id: str, wait: int = Query(0, ge=0, le=60, description="Seconds to wait for the run to finish"),
                p: Principal = Depends(require("gate.read")), db: Session = Depends(tenant_db)):
    run = get_or_404(db, TestRun, run_id, "run")
    _scope(p, run.project_id)
    deadline = time.monotonic() + wait
    while run.status not in FINISHED and time.monotonic() < deadline:
        db.commit()  # end the snapshot so the next read sees the worker's progress
        time.sleep(1)
        db.refresh(run)
    return _result(db, run)


# ---- pipeline keys (admin) -------------------------------------------------------------------
class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    project_id: uuid.UUID
    expires_in_days: int = Field(90, ge=1, le=365)


@router.post("/api-keys", status_code=201)
def create_api_key(body: KeyIn, p: Principal = Depends(require("api_key.manage")), db: Session = Depends(tenant_db)):
    """The key is returned once. It can only call the release gate, for this project."""
    project = get_or_404(db, Project, body.project_id, "project")
    k, token = create_key(db, p.org_id, p.user_id, body.name, project.id, body.expires_in_days)
    record_for(db, p, "api_key.create", "api_key", k.id,
               {"key_id": k.key_id, "name": k.name, "project_id": str(project.id), "expires_at": k.expires_at.isoformat()})
    db.commit()
    return {**public(k), "key": token,
            "note": "Store this key in your pipeline's secret store now; it is not shown again."}


@router.get("/api-keys")
def list_api_keys(p: Principal = Depends(require("api_key.manage")), db: Session = Depends(tenant_db)):
    return [public(k) for k in db.scalars(select(ApiKey).order_by(ApiKey.created_at.desc()))]


@router.post("/api-keys/{key_uuid}/revoke")
def revoke_api_key(key_uuid: str, p: Principal = Depends(require("api_key.manage")), db: Session = Depends(tenant_db)):
    k = get_or_404(db, ApiKey, key_uuid, "API key")
    if k.revoked_at is not None:
        raise conflict("key already revoked")
    revoke_key(db, k, p.user_id)
    record_for(db, p, "api_key.revoke", "api_key", k.id, {"key_id": k.key_id, "name": k.name})
    db.commit()
    return public(k)
