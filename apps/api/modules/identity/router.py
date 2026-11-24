from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.deps import tenant_db
from common.errors import ApiError
from db.models import Membership, Organization, User
from db.session import new_session
from modules.audit.service import record
from modules.identity.auth import Principal, issue_dev_token, require, verify_password, current_principal
from settings import get_settings

router = APIRouter(prefix="/v1", tags=["identity"])


class LoginIn(BaseModel):
    email: str
    password: str
    organization_id: uuid.UUID | None = None


@router.post("/auth/login")
def login(body: LoginIn):
    """Development login. Disabled when MA_AUTH_MODE=oidc (tokens then come from the IdP)."""
    if get_settings().auth_mode != "dev":
        raise ApiError(404, "NOT_FOUND", "password login is disabled; authenticate with the identity provider")
    with new_session() as db:
        user = db.scalar(select(User).where(User.email == body.email.lower().strip()))
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        raise ApiError(401, "UNAUTHENTICATED", "invalid email or password")
    with new_session(None, user.id) as db:
        orgs = list(db.execute(
            select(Organization.id, Organization.name, Membership.role)
            .join(Membership, Membership.organization_id == Organization.id)
            .where(Membership.user_id == user.id)
        ))
    if not orgs:
        raise ApiError(403, "FORBIDDEN", "user has no organisation membership")
    org_ids = {o[0] for o in orgs}
    org_id = body.organization_id or (next(iter(org_ids)) if len(org_ids) == 1 else None)
    if org_id is None:
        raise ApiError(400, "ORG_REQUIRED", "choose an organisation", [{"id": str(o[0]), "name": o[1]} for o in orgs])
    if org_id not in org_ids:
        raise ApiError(403, "FORBIDDEN", "not a member of that organisation")
    with new_session(org_id, user.id) as db:
        record(db, org_id=org_id, actor_id=user.id, actor_label=user.email, action="auth.login",
               resource_type="user", resource_id=user.id)
        db.commit()
    return {"access_token": issue_dev_token(user, org_id), "token_type": "bearer",
            "organization_id": str(org_id),
            "roles": sorted(r for (oid, _, r) in orgs if oid == org_id)}


@router.get("/me")
def me(p: Principal = Depends(current_principal), db: Session = Depends(tenant_db)):
    org = db.get(Organization, p.org_id)
    return {"id": str(p.user_id), "email": p.email, "name": p.name, "roles": sorted(p.roles),
            "organization": {"id": str(p.org_id), "name": org.name if org else None}, "mfa": p.mfa}


@router.get("/users")
def list_users(p: Principal = Depends(require("user.read")), db: Session = Depends(tenant_db)):
    rows = db.execute(
        select(User.id, User.email, User.name, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.organization_id == p.org_id)
        .order_by(User.name)
    )
    users: dict[str, dict] = {}
    for uid, email, name, role in rows:
        users.setdefault(str(uid), {"id": str(uid), "email": email, "name": name, "roles": []})["roles"].append(role)
    return list(users.values())
