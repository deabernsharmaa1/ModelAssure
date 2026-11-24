"""Append-only, tamper-evident audit trail (FR-05, spec §12.2 Auditability).

Each organisation has its own hash chain:
    hash_n = SHA-256(prev_hash || canonical(event_n))
Appends are serialised per organisation with a transaction-level advisory lock. The table is
append-only (trigger + REVOKE), and `verify_chain` recomputes the chain to detect tampering.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from common.hashing import content_hash, sha256_hex
from db.models import AuditEvent
from db.session import new_session

GENESIS = "0" * 64


def _payload(ev: AuditEvent) -> dict[str, Any]:
    return {
        "organization_id": str(ev.organization_id), "seq": ev.seq,
        "actor_id": str(ev.actor_id) if ev.actor_id else None, "actor_label": ev.actor_label,
        "action": ev.action, "resource_type": ev.resource_type, "resource_id": ev.resource_id,
        "outcome": ev.outcome, "details": ev.details,
        "timestamp": ev.timestamp.astimezone(timezone.utc).isoformat(),
    }


def record(db: Session, *, org_id, actor_id, actor_label: str, action: str, resource_type: str,
           resource_id: Any = None, details: dict[str, Any] | None = None, outcome: str = "SUCCESS") -> AuditEvent:
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"audit:{org_id}"})
    last = db.execute(
        select(AuditEvent.seq, AuditEvent.hash).where(AuditEvent.organization_id == org_id)
        .order_by(AuditEvent.seq.desc()).limit(1)
    ).first()
    seq, prev = (last[0] + 1, last[1]) if last else (1, GENESIS)
    ev = AuditEvent(
        id=uuid.uuid4(), organization_id=uuid.UUID(str(org_id)), seq=seq,
        actor_id=uuid.UUID(str(actor_id)) if actor_id else None, actor_label=actor_label,
        action=action, resource_type=resource_type, resource_id=str(resource_id) if resource_id else None,
        outcome=outcome, details=details or {}, timestamp=datetime.now(timezone.utc).replace(microsecond=0),
        prev_hash=prev,
    )
    ev.hash = sha256_hex(prev + content_hash(_payload(ev)))
    db.add(ev)
    db.flush()
    return ev


def record_for(db: Session, principal, action: str, resource_type: str, resource_id: Any = None,
               details: dict[str, Any] | None = None, outcome: str = "SUCCESS") -> AuditEvent:
    return record(db, org_id=principal.org_id, actor_id=principal.user_id, actor_label=principal.email,
                  action=action, resource_type=resource_type, resource_id=resource_id, details=details, outcome=outcome)


def record_denied(principal, permission: str, path: str, reason: str = "PERMISSION_DENIED") -> None:
    """Denied actions are logged in their own transaction so the request's rollback can't erase them."""
    with new_session(principal.org_id, principal.user_id) as db:
        record_for(db, principal, "access.denied", "endpoint", path,
                   {"permission": permission, "reason": reason, "roles": sorted(principal.roles)}, outcome="DENIED")
        db.commit()


def verify_chain(db: Session, org_id) -> dict[str, Any]:
    prev, n = GENESIS, 0
    for ev in db.scalars(select(AuditEvent).where(AuditEvent.organization_id == org_id).order_by(AuditEvent.seq)):
        n += 1
        if ev.seq != n:
            return {"valid": False, "checked": n, "problem": f"sequence gap at {n} (found {ev.seq})"}
        if ev.prev_hash != prev:
            return {"valid": False, "checked": n, "problem": f"chain broken at seq {ev.seq}"}
        if sha256_hex(prev + content_hash(_payload(ev))) != ev.hash:
            return {"valid": False, "checked": n, "problem": f"event {ev.seq} content does not match its hash"}
        prev = ev.hash
    return {"valid": True, "checked": n, "head": prev}
