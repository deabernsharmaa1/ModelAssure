"""Pipeline API keys and who is accountable for what a key does."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import ApiKey, Membership, User
from modules.identity.auth import API_KEY_PREFIX, CI_ROLE


def create_key(db: Session, org_id, creator_id, name: str, project_id, expires_in_days: int) -> tuple[ApiKey, str]:
    key_id = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    user = User(email=f"ci-{key_id}@service.modelassure.invalid", name=f"Pipeline: {name}", password_hash=None)
    db.add(user)
    db.flush()
    db.add(Membership(user_id=user.id, organization_id=org_id, role=CI_ROLE))
    k = ApiKey(organization_id=org_id, key_id=key_id, name=name, secret_hash=hashlib.sha256(secret.encode()).hexdigest(),
               service_user_id=user.id, project_id=project_id, created_by=creator_id,
               expires_at=datetime.now(timezone.utc) + timedelta(days=expires_in_days))
    db.add(k)
    db.flush()
    return k, f"{API_KEY_PREFIX}{key_id}_{secret}"


def revoke_key(db: Session, k: ApiKey, by) -> None:
    k.revoked_at = datetime.now(timezone.utc)
    k.revoked_by = by
    svc = db.get(User, k.service_user_id)
    if svc is not None:
        svc.is_active = False
    db.flush()


def accountable(db: Session, user_id) -> set[uuid.UUID]:
    """The people answerable for an action: the actor, plus whoever created the pipeline key when
    the actor is a key's service identity. Used for segregation of duties."""
    if user_id is None:
        return set()
    out = {user_id}
    out |= {c for c in db.scalars(select(ApiKey.created_by).where(ApiKey.service_user_id == user_id)) if c}
    return out


def public(k: ApiKey) -> dict:
    now = datetime.now(timezone.utc)
    return {"id": str(k.id), "key_id": k.key_id, "prefix": f"{API_KEY_PREFIX}{k.key_id}_…", "name": k.name,
            "project_id": str(k.project_id), "created_by": str(k.created_by) if k.created_by else None,
            "created_at": k.created_at.isoformat(), "expires_at": k.expires_at.isoformat(),
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
            "status": "REVOKED" if k.revoked_at else ("EXPIRED" if k.expires_at <= now else "ACTIVE")}
