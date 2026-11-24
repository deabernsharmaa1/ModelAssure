"""Authentication, roles and permissions (FR-02, spec §12.2).

Two modes:
  dev   Local email/password login issuing short-lived HS256 tokens. For demos and tests only.
  oidc  Bearer tokens from an OAuth 2.0 / OIDC identity provider, verified against its JWKS.
        The token `sub` maps to users.identity_provider_id; MFA is read from the `amr` claim.

Roles are always loaded from the database on each request, so revoking a membership takes
effect immediately.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, Header, Request
from sqlalchemy import select, text

from common.errors import ApiError
from db.models import Membership, User
from db.session import new_session
from settings import get_settings

ROLES = ("ADMIN", "RISK_MANAGER", "ENGINEER", "ANALYST", "REVIEWER", "AUDITOR")
CI_ROLE = "CI"  # service identity of a pipeline API key; deliberately not in ROLES

# Least-privilege permission matrix.
PERMISSIONS: dict[str, set[str]] = {
    "project.create": {"ADMIN", "RISK_MANAGER"},
    "project.read": set(ROLES),
    "system.manage": {"ADMIN", "ENGINEER"},
    "connection.manage": {"ADMIN", "ENGINEER"},
    "dataset.manage": {"ADMIN", "ENGINEER", "ANALYST"},
    "policy.edit": {"ADMIN", "RISK_MANAGER"},
    "policy.approve": {"RISK_MANAGER", "REVIEWER"},
    "suite.create": {"ADMIN", "RISK_MANAGER", "ANALYST", "ENGINEER"},
    "run.create": {"ADMIN", "RISK_MANAGER", "ANALYST", "ENGINEER"},
    "finding.update": {"ADMIN", "RISK_MANAGER", "ANALYST", "ENGINEER"},
    "finding.accept_risk": {"RISK_MANAGER"},
    "finding.close": {"RISK_MANAGER", "ADMIN"},
    "finding.severity": {"RISK_MANAGER"},
    "release.review": {"REVIEWER"},
    "report.read": set(ROLES),
    "audit.read": {"AUDITOR", "ADMIN", "RISK_MANAGER"},
    # Packs leave the organisation, so the people who build the system do not issue them.
    "evidence_pack.create": {"ADMIN", "RISK_MANAGER", "REVIEWER", "AUDITOR"},
    "signing_key.manage": {"ADMIN"},
    # Release gate. CI is the role of a pipeline key's service identity and holds nothing else.
    "gate.run": {CI_ROLE, "ADMIN", "ENGINEER"},
    "gate.read": {CI_ROLE, *ROLES},
    "api_key.manage": {"ADMIN"},
    "user.read": set(ROLES),
}
PRIVILEGED = {"policy.approve", "finding.accept_risk", "release.review", "finding.close"}


@dataclass
class Principal:
    user_id: uuid.UUID
    email: str
    name: str
    org_id: uuid.UUID
    roles: set[str] = field(default_factory=set)
    mfa: bool = False
    api_key_id: uuid.UUID | None = None      # set when authenticated with a pipeline key
    scope_project_id: uuid.UUID | None = None  # a pipeline key may act on this project only

    def can(self, permission: str) -> bool:
        return bool(self.roles & PERMISSIONS[permission])


# ---- password hashing (stdlib scrypt; dev mode only) ----------------------------------------
def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    _, salt_hex, dk_hex = stored.split("$")
    dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(dk.hex(), dk_hex)


# ---- tokens -----------------------------------------------------------------------------------
def issue_dev_token(user: User, org_id: uuid.UUID, mfa: bool = False) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    claims = {"sub": str(user.id), "org": str(org_id), "iat": now, "exp": now + timedelta(minutes=s.jwt_ttl_minutes),
              "iss": "modelassure-dev", "amr": ["pwd", "mfa"] if mfa else ["pwd"]}
    return jwt.encode(claims, s.jwt_secret, algorithm="HS256")


_jwks_client = None


def _decode(token: str) -> dict:
    s = get_settings()
    if s.auth_mode == "dev":
        return jwt.decode(token, s.jwt_secret, algorithms=["HS256"], issuer="modelassure-dev")
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(s.oidc_jwks_url)
    key = _jwks_client.get_signing_key_from_jwt(token).key
    return jwt.decode(token, key, algorithms=["RS256", "ES256"], audience=s.oidc_audience, issuer=s.oidc_issuer)


def load_principal(token: str, org_header: str | None) -> Principal:
    try:
        claims = _decode(token)
    except jwt.PyJWTError as e:
        raise ApiError(401, "UNAUTHENTICATED", f"invalid token: {e}") from None
    s = get_settings()
    with new_session() as db:
        if s.auth_mode == "dev":
            user = db.get(User, uuid.UUID(claims["sub"]))
            org_raw = claims.get("org")
        else:
            user = db.scalar(select(User).where(User.identity_provider_id == claims["sub"]))
            org_raw = org_header
        if user is None or not user.is_active:
            raise ApiError(401, "UNAUTHENTICATED", "unknown or inactive user")
        try:
            org_id = uuid.UUID(str(org_raw))
        except (ValueError, TypeError):
            raise ApiError(401, "UNAUTHENTICATED", "organisation not specified") from None
    # Memberships are RLS-protected: read them inside the user's own context.
    with new_session(org_id, user.id) as db:
        roles = set(db.scalars(select(Membership.role).where(Membership.user_id == user.id,
                                                             Membership.organization_id == org_id)))
    if not roles:
        raise ApiError(403, "FORBIDDEN", "not a member of this organisation")
    return Principal(user.id, user.email, user.name, org_id, roles, mfa="mfa" in (claims.get("amr") or []))


API_KEY_PREFIX = "mak_"


def load_api_key_principal(token: str) -> Principal:
    """`mak_<key id>_<secret>`. Only a SHA-256 of the secret is stored."""
    parts = token.split("_", 2)  # the secret itself may contain underscores
    if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{12}", parts[1]) or len(parts[2]) < 32:
        raise ApiError(401, "UNAUTHENTICATED", "malformed API key")
    with new_session() as db:
        k = db.execute(text("SELECT * FROM ma_lookup_api_key(:k)"), {"k": parts[1]}).mappings().first()
    if k is None or not hmac.compare_digest(hashlib.sha256(parts[2].encode()).hexdigest(), k["secret_hash"]):
        raise ApiError(401, "UNAUTHENTICATED", "invalid API key")
    now = datetime.now(timezone.utc)
    if k["revoked_at"] is not None:
        raise ApiError(401, "UNAUTHENTICATED", "API key has been revoked")
    if k["expires_at"] <= now:
        raise ApiError(401, "UNAUTHENTICATED", "API key has expired")
    with new_session(k["organization_id"], k["service_user_id"]) as db:
        user = db.get(User, k["service_user_id"])
        if user is None or not user.is_active:
            raise ApiError(401, "UNAUTHENTICATED", "API key identity is inactive")
        db.execute(text("UPDATE api_keys SET last_used_at = now() WHERE id = :i AND "
                        "(last_used_at IS NULL OR last_used_at < now() - interval '1 minute')"), {"i": k["id"]})
        db.commit()
        return Principal(user.id, user.email, user.name, k["organization_id"], {CI_ROLE},
                         api_key_id=k["id"], scope_project_id=k["project_id"])


def current_principal(request: Request, authorization: str = Header(default=""),
                      x_organization_id: str | None = Header(default=None)) -> Principal:
    if not authorization.lower().startswith("bearer "):
        raise ApiError(401, "UNAUTHENTICATED", "missing bearer token")
    token = authorization[7:].strip()
    p = load_api_key_principal(token) if token.startswith(API_KEY_PREFIX) else load_principal(token, x_organization_id)
    request.state.principal = p
    return p


def require(permission: str):
    """FastAPI dependency: the caller must hold `permission`. Denials are audit-logged."""

    def dep(request: Request, p: Principal = Depends(current_principal)) -> Principal:
        if not p.can(permission):
            from modules.audit.service import record_denied

            record_denied(p, permission, str(request.url.path))
            raise ApiError(403, "FORBIDDEN", f"permission '{permission}' required")
        if permission in PRIVILEGED and get_settings().require_mfa_for_privileged and not p.mfa:
            from modules.audit.service import record_denied

            record_denied(p, permission, str(request.url.path), reason="MFA_REQUIRED")
            raise ApiError(403, "MFA_REQUIRED", "this action requires multi-factor authentication")
        return p

    return dep
