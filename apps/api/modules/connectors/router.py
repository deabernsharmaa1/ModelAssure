"""Decision-system connections (Module B: FR-06 – FR-10)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.deps import get_or_404, row, tenant_db
from common.errors import invalid
from common.hashing import content_hash
from db.models import Connection, Dataset, Organization, SystemVersion
from modules.audit.service import record_for
from modules.connectors.client import DecisionClient
from modules.connectors.egress import EgressError, check_url
from modules.connectors.mapping import MappingError, validate_mapping
from modules.connectors.secrets import SecretError, resolve_secret, store_secret
from modules.identity.auth import Principal, require
from modules.scenarios.generators import DEFAULT_BASE_CASE
from settings import get_settings

router = APIRouter(prefix="/v1", tags=["connections"])


class ConnectionIn(BaseModel):
    system_version_id: uuid.UUID
    kind: Literal["REST", "RECORDED"] = "REST"
    environment: Literal["SANDBOX"] = Field("SANDBOX", description="Only sandbox/test environments may be registered")
    endpoint: str | None = None
    auth_header: str | None = Field(default=None, max_length=100)
    credential: str | None = Field(default=None, description="Stored in the secret store; never persisted in tables or logs")
    credential_ref: str | None = Field(default=None, description="Existing secret reference, e.g. env:BANK_SANDBOX_KEY")
    mapping: dict[str, Any] = Field(default_factory=dict)
    timeout_ms: int = Field(5000, ge=100, le=120_000)
    max_concurrency: int | None = Field(default=None, ge=1)
    max_rps: float | None = Field(default=None, gt=0, le=1000)
    dependency_control_url: str | None = None
    dependency_credential: str | None = None
    recorded_dataset_id: uuid.UUID | None = None


def _public(c: Connection) -> dict[str, Any]:
    d = row(c)
    d["has_credential"] = bool(d.pop("secret_ref"))
    d["has_dependency_credential"] = bool(d.pop("dependency_secret_ref"))
    return d


@router.post("/connections", status_code=201)
def create_connection(body: ConnectionIn, p: Principal = Depends(require("connection.manage")), db: Session = Depends(tenant_db)):
    s = get_settings()
    version = get_or_404(db, SystemVersion, body.system_version_id, "system version")
    org = db.get(Organization, p.org_id)
    try:
        mapping = validate_mapping(body.mapping)
    except MappingError as e:
        raise invalid(f"invalid mapping: {e}") from None
    concurrency = body.max_concurrency or s.default_connection_concurrency
    if concurrency > s.max_connection_concurrency:
        raise invalid(f"max_concurrency may not exceed {s.max_connection_concurrency}")
    if body.kind == "REST":
        if not body.endpoint:
            raise invalid("endpoint is required for REST connections")
        try:
            check_url(body.endpoint, org.approved_destinations if org else [])
            if body.dependency_control_url:
                check_url(body.dependency_control_url, org.approved_destinations if org else [])
        except EgressError as e:
            raise invalid(f"endpoint not permitted: {e}") from None
    else:
        if not body.recorded_dataset_id:
            raise invalid("recorded_dataset_id is required for RECORDED connections")
        ds = get_or_404(db, Dataset, body.recorded_dataset_id, "dataset")
        if ds.kind != "RECORDED_OUTPUTS":
            raise invalid("dataset must be of kind RECORDED_OUTPUTS")
    if body.credential and body.credential_ref:
        raise invalid("give either credential or credential_ref, not both")
    secret_ref = store_secret(body.credential) if body.credential else body.credential_ref
    if secret_ref:
        try:
            resolve_secret(secret_ref)
        except SecretError as e:
            raise invalid(str(e)) from None
    dep_ref = store_secret(body.dependency_credential) if body.dependency_credential else None

    c = Connection(
        organization_id=p.org_id, system_version_id=version.id, kind=body.kind, endpoint=body.endpoint,
        environment=body.environment, auth_header=body.auth_header, secret_ref=secret_ref, mapping=mapping,
        mapping_hash=content_hash(mapping), timeout_ms=body.timeout_ms, max_concurrency=concurrency,
        max_rps=body.max_rps, dependency_control_url=body.dependency_control_url, dependency_secret_ref=dep_ref,
        recorded_dataset_id=body.recorded_dataset_id, created_by=p.user_id,
    )
    db.add(c)
    db.flush()
    record_for(db, p, "connection.create", "connection", c.id,
               {"system_version_id": str(version.id), "kind": c.kind, "endpoint": c.endpoint, "mapping_hash": c.mapping_hash})
    db.commit()
    return _public(c)


@router.get("/connections")
def list_connections(system_version_id: uuid.UUID | None = None, p: Principal = Depends(require("project.read")),
                     db: Session = Depends(tenant_db)):
    q = select(Connection).order_by(Connection.created_at.desc())
    if system_version_id:
        q = q.where(Connection.system_version_id == system_version_id)
    return [_public(c) for c in db.scalars(q)]


@router.get("/connections/{connection_id}")
def get_connection(connection_id: str, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    return _public(get_or_404(db, Connection, connection_id, "connection"))


def connection_snapshot(c: Connection, v: SystemVersion) -> dict[str, Any]:
    """The configuration that is locked into a run (no secrets, only references)."""
    return {
        "connection_id": str(c.id), "kind": c.kind, "endpoint": c.endpoint, "auth_header": c.auth_header,
        "secret_ref": c.secret_ref, "mapping": c.mapping, "mapping_hash": c.mapping_hash,
        "timeout_ms": c.timeout_ms, "max_concurrency": c.max_concurrency, "max_rps": c.max_rps,
        "dependency_control_url": c.dependency_control_url, "dependency_secret_ref": c.dependency_secret_ref,
        "recorded_dataset_id": str(c.recorded_dataset_id) if c.recorded_dataset_id else None,
        "system_version_id": str(v.id), "version": v.version, "version_checksum": v.checksum,
        "expected_model_version": v.expected_model_version,
    }


@router.post("/connections/{connection_id}/verify")
def verify_connection(connection_id: str, p: Principal = Depends(require("connection.manage")), db: Session = Depends(tenant_db)):
    """FR-08: validate authentication, request mapping, response schema and reported version."""
    c = get_or_404(db, Connection, connection_id, "connection")
    v = db.get(SystemVersion, c.system_version_id)
    org = db.get(Organization, p.org_id)
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, level: str = "error") -> None:
        checks.append({"check": name, "passed": ok, "detail": detail, "level": "info" if ok else level})

    if c.kind == "RECORDED":
        add("recorded_dataset", c.recorded_dataset_id is not None, "recorded outputs dataset linked")
    else:
        try:
            check_url(c.endpoint, org.approved_destinations if org else [])
            add("egress_policy", True, "endpoint is an approved test destination")
        except EgressError as e:
            add("egress_policy", False, str(e))
        try:
            cred = resolve_secret(c.secret_ref)
            add("credential", True, "credential reference resolves" if cred else "no credential configured")
        except SecretError as e:
            cred = None
            add("credential", False, str(e))
        if all(ch["passed"] for ch in checks):
            snap = connection_snapshot(c, v)
            probe = {**DEFAULT_BASE_CASE, "case_id": f"MA-VERIFY-{uuid.uuid4().hex[:8]}"}
            client = DecisionClient(snap, cred)
            try:
                obs = client.call(probe, case_id=probe["case_id"])
            finally:
                client.close()
            if obs.status == "OK":
                add("request_and_schema", True, f"decision {obs.decision} returned and normalised ({obs.duration_ms} ms)")
                add("reported_version", True, f"model_version={obs.normalized.get('model_version')!r}")
            elif obs.error_type == "VERSION_MISMATCH":
                add("request_and_schema", True, "response normalised")
                add("reported_version", False, obs.error_detail)
            elif obs.error_type == "AUTH_ERROR":
                add("authentication", False, obs.error_detail)
            elif obs.status == "REJECTED":
                add("request_mapping", False, f"sample request rejected ({obs.error_detail}): {str(obs.raw_response)[:500]}")
            else:
                add("request_and_schema", False, f"{obs.error_type}: {obs.error_detail}")
            # Authentication must actually be enforced by the sandbox.
            if c.auth_header and cred:
                unauth = DecisionClient({**snap, "auth_header": None}, None)
                try:
                    o2 = unauth.call(probe, case_id=probe["case_id"])
                finally:
                    unauth.close()
                add("auth_enforced", o2.error_type == "AUTH_ERROR",
                    "endpoint rejects unauthenticated requests" if o2.error_type == "AUTH_ERROR"
                    else "endpoint accepted a request without credentials", level="warning")
        if c.dependency_control_url:
            try:
                dep_key = resolve_secret(c.dependency_secret_ref)
                r = httpx.put(f"{c.dependency_control_url.rstrip('/')}/control/scenarios", json={"scenarios": []},
                              headers={"X-Control-Key": dep_key or ""}, timeout=5, follow_redirects=False)
                add("dependency_simulator", r.status_code == 200, f"control endpoint answered HTTP {r.status_code}")
            except (httpx.HTTPError, SecretError) as e:
                add("dependency_simulator", False, f"control endpoint unreachable: {e}")

    ok = all(ch["passed"] or ch["level"] == "warning" for ch in checks)
    c.status = "VERIFIED" if ok else "FAILED"
    c.verification = {"checks": checks, "verified_by": p.email}
    c.verified_at = datetime.now(timezone.utc)
    record_for(db, p, "connection.verify", "connection", c.id, {"status": c.status, "checks": checks})
    db.commit()
    return {"status": c.status, "checks": checks}
