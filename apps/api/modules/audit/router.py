from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.deps import row, tenant_db
from db.models import AuditEvent
from modules.audit.service import verify_chain
from modules.identity.auth import Principal, require

router = APIRouter(prefix="/v1", tags=["audit"])


@router.get("/audit-events")
def list_events(action: str | None = None, resource_type: str | None = None, resource_id: str | None = None,
                limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                p: Principal = Depends(require("audit.read")), db: Session = Depends(tenant_db)):
    q = select(AuditEvent)
    if action:
        q = q.where(AuditEvent.action == action)
    if resource_type:
        q = q.where(AuditEvent.resource_type == resource_type)
    if resource_id:
        q = q.where(AuditEvent.resource_id == resource_id)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    items = db.scalars(q.order_by(AuditEvent.seq.desc()).limit(limit).offset(offset))
    return {"total": total, "items": [row(e) for e in items]}


@router.get("/audit-events/verify")
def verify(p: Principal = Depends(require("audit.read")), db: Session = Depends(tenant_db)):
    return verify_chain(db, p.org_id)
