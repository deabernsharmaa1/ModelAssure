"""Projects, financial systems and immutable version registry (Module A: FR-01, FR-03, FR-04)."""
from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.deps import get_or_404, row, tenant_db
from common.errors import conflict, invalid
from common.hashing import content_hash
from db.models import Finding, Membership, Project, System, SystemVersion, TestRun
from modules.audit.service import record_for
from modules.identity.auth import Principal, require

router = APIRouter(prefix="/v1", tags=["projects"])


# ---- systems & versions -----------------------------------------------------------------------
class SystemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    category: Literal["SME_LENDING_DECISION"] = "SME_LENDING_DECISION"  # Version 1 scope (spec §1.4)
    vendor: str | None = None


@router.post("/systems", status_code=201)
def create_system(body: SystemIn, p: Principal = Depends(require("system.manage")), db: Session = Depends(tenant_db)):
    s = System(organization_id=p.org_id, name=body.name, category=body.category, vendor=body.vendor, created_by=p.user_id)
    db.add(s)
    try:
        db.flush()
    except IntegrityError:
        raise conflict(f"a system named '{body.name}' already exists") from None
    record_for(db, p, "system.create", "system", s.id, {"name": s.name})
    db.commit()
    return row(s)


@router.get("/systems")
def list_systems(p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    return [row(s) for s in db.scalars(select(System).order_by(System.created_at))]


class VersionIn(BaseModel):
    version: str = Field(min_length=1, max_length=100)
    expected_model_version: str | None = Field(default=None, description="model_version the endpoint must report (FR-10)")
    artifact_digest: str | None = Field(default=None, description="e.g. container image digest supplied by the customer")
    description: str | None = None


@router.post("/systems/{system_id}/versions", status_code=201)
def register_version(system_id: str, body: VersionIn, p: Principal = Depends(require("system.manage")),
                     db: Session = Depends(tenant_db)):
    system = get_or_404(db, System, system_id, "system")
    v = SystemVersion(organization_id=p.org_id, system_id=system.id, version=body.version,
                      expected_model_version=body.expected_model_version, artifact_digest=body.artifact_digest,
                      description=body.description, registered_by=p.user_id,
                      checksum=content_hash({"system_id": str(system.id), **body.model_dump()}))
    db.add(v)
    try:
        db.flush()
    except IntegrityError:
        raise conflict(f"version '{body.version}' is already registered for this system (versions are immutable)") from None
    record_for(db, p, "system_version.register", "system_version", v.id, {"system": system.name, "version": v.version, "checksum": v.checksum})
    db.commit()
    return row(v)


@router.get("/systems/{system_id}/versions")
def list_versions(system_id: str, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    get_or_404(db, System, system_id, "system")
    return [row(v) for v in db.scalars(select(SystemVersion).where(SystemVersion.system_id == uuid.UUID(system_id))
                                       .order_by(SystemVersion.created_at))]


# ---- projects ----------------------------------------------------------------------------------
class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    business_use_case: str = Field(min_length=1)
    system_id: uuid.UUID
    owner_id: uuid.UUID
    baseline_version_id: uuid.UUID
    candidate_version_id: uuid.UUID
    risk_classification: Literal["HIGH", "MEDIUM", "LOW"]
    test_environment: str = Field(min_length=1, max_length=200)


class ProjectPatch(BaseModel):
    candidate_version_id: uuid.UUID | None = None
    baseline_version_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    status: Literal["ACTIVE", "ARCHIVED"] | None = None


def _check_versions(db: Session, system_id, *version_ids) -> None:
    for vid in version_ids:
        if vid is None:
            continue
        v = db.get(SystemVersion, vid)
        if v is None or v.system_id != system_id:
            raise invalid(f"version {vid} is not registered for the project's system")


def _check_member(db: Session, org_id, user_id) -> None:
    if not db.scalar(select(Membership.id).where(Membership.organization_id == org_id, Membership.user_id == user_id).limit(1)):
        raise invalid("owner must be a member of this organisation")


@router.post("/projects", status_code=201)
def create_project(body: ProjectIn, p: Principal = Depends(require("project.create")), db: Session = Depends(tenant_db)):
    get_or_404(db, System, body.system_id, "system")
    _check_versions(db, body.system_id, body.baseline_version_id, body.candidate_version_id)
    if body.baseline_version_id == body.candidate_version_id:
        raise invalid("baseline and candidate must be different versions")
    _check_member(db, p.org_id, body.owner_id)
    proj = Project(organization_id=p.org_id, created_by=p.user_id, **body.model_dump())
    db.add(proj)
    db.flush()
    record_for(db, p, "project.create", "project", proj.id, {"name": proj.name})
    db.commit()
    return row(proj)


@router.get("/projects")
def list_projects(p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    out = []
    for proj in db.scalars(select(Project).order_by(Project.created_at.desc())):
        d = row(proj)
        d["open_findings"] = db.scalar(select(func.count()).select_from(Finding).where(
            Finding.project_id == proj.id, Finding.status.notin_(["RESOLVED", "CLOSED", "ACCEPTED_RISK"]))) or 0
        latest = db.scalars(select(TestRun).where(TestRun.project_id == proj.id).order_by(TestRun.created_at.desc()).limit(1)).first()
        d["latest_run"] = ({"id": str(latest.id), "run_number": latest.run_number, "status": latest.status,
                            "assessment": (latest.assessment or {}).get("outcome")} if latest else None)
        out.append(d)
    return out


@router.get("/projects/{project_id}")
def get_project(project_id: str, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    proj = get_or_404(db, Project, project_id, "project")
    d = row(proj)
    d["system"] = row(db.get(System, proj.system_id)) if proj.system_id else None
    d["baseline_version"] = row(db.get(SystemVersion, proj.baseline_version_id)) if proj.baseline_version_id else None
    d["candidate_version"] = row(db.get(SystemVersion, proj.candidate_version_id)) if proj.candidate_version_id else None
    return d


@router.patch("/projects/{project_id}")
def update_project(project_id: str, body: ProjectPatch, p: Principal = Depends(require("project.create")),
                   db: Session = Depends(tenant_db)):
    proj = get_or_404(db, Project, project_id, "project")
    changes = body.model_dump(exclude_none=True)
    _check_versions(db, proj.system_id, changes.get("baseline_version_id"), changes.get("candidate_version_id"))
    if "owner_id" in changes:
        _check_member(db, p.org_id, changes["owner_id"])
    before = {k: str(getattr(proj, k)) for k in changes}
    for k, v in changes.items():
        setattr(proj, k, v)
    if proj.baseline_version_id == proj.candidate_version_id:
        raise invalid("baseline and candidate must be different versions")
    record_for(db, p, "project.update", "project", proj.id, {"before": before, "after": {k: str(v) for k, v in changes.items()}})
    db.commit()
    return row(proj)
