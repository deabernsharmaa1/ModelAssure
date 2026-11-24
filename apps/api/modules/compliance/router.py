"""Regulatory mapping API."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from common.deps import get_or_404, tenant_db
from common.errors import invalid
from db.models import TestRun
from modules.compliance.mapping import DISCLAIMER, evaluate, load_frameworks
from modules.identity.auth import Principal, require

router = APIRouter(prefix="/v1", tags=["regulatory mapping"])


@router.get("/compliance/frameworks")
def frameworks(p: Principal = Depends(require("project.read"))):
    return {"disclaimer": DISCLAIMER, "frameworks": list(load_frameworks().values())}


@router.get("/runs/{run_id}/compliance")
def run_compliance(run_id: str, framework: list[str] | None = Query(default=None),
                   p: Principal = Depends(require("report.read")), db: Session = Depends(tenant_db)):
    run = get_or_404(db, TestRun, run_id, "run")
    known = set(load_frameworks())
    if framework and not set(framework) <= known:
        raise invalid(f"unknown framework(s) {sorted(set(framework) - known)}; known: {sorted(known)}")
    return evaluate(db, run, framework)
