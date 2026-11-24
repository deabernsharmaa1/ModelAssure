"""Per-organisation Ed25519 signing keys for evidence packs."""
from __future__ import annotations

import base64
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.hashing import sha256_hex
from db.models import SigningKey
from modules.connectors.secrets import resolve_secret, store_secret


def fingerprint_of(public_key) -> str:
    return sha256_hex(public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def active_key(db: Session, org_id) -> SigningKey | None:
    return db.scalars(select(SigningKey).where(SigningKey.organization_id == org_id, SigningKey.status == "ACTIVE")).first()


def create_key(db: Session, org_id, user_id) -> SigningKey:
    priv = Ed25519PrivateKey.generate()
    pem_priv = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()).decode()
    pub = priv.public_key()
    k = SigningKey(organization_id=org_id, public_key_pem=pub.public_bytes(serialization.Encoding.PEM,
                   serialization.PublicFormat.SubjectPublicKeyInfo).decode(),
                   fingerprint=fingerprint_of(pub), secret_ref=store_secret(pem_priv), created_by=user_id)
    db.add(k)
    db.flush()
    return k


def get_or_create_key(db: Session, org_id, user_id) -> SigningKey:
    k = active_key(db, org_id)
    if k is not None:
        return k
    try:
        with db.begin_nested():
            return create_key(db, org_id, user_id)
    except IntegrityError:  # another request created it first
        k = active_key(db, org_id)
        assert k is not None
        return k


def rotate(db: Session, org_id, user_id) -> tuple[SigningKey | None, SigningKey]:
    old = active_key(db, org_id)
    if old is not None:
        old.status = "RETIRED"
        old.retired_at = datetime.now(timezone.utc)
        db.flush()
    return old, create_key(db, org_id, user_id)


def sign(key: SigningKey, data: bytes) -> str:
    pem = resolve_secret(key.secret_ref)
    priv = serialization.load_pem_private_key(pem.encode(), password=None)
    assert isinstance(priv, Ed25519PrivateKey)
    return base64.b64encode(priv.sign(data)).decode()
