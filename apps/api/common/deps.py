"""Shared FastAPI dependencies and serialisation helpers."""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterator

from fastapi import Depends
from sqlalchemy.orm import Session

from common.errors import not_found
from db.session import new_session
from modules.identity.auth import Principal, current_principal


def tenant_db(p: Principal = Depends(current_principal)) -> Iterator[Session]:
    """A session whose every transaction is scoped to the caller's organisation (RLS).
    Endpoints commit explicitly; anything uncommitted is rolled back."""
    db = new_session(p.org_id, p.user_id)
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def to_json(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_json(v) for v in value]
    return value


def row(obj: Any, exclude: set[str] | None = None) -> dict[str, Any]:
    exclude = exclude or set()
    out = {}
    for col in obj.__table__.columns:
        key = col.key if hasattr(col, "key") else col.name
        attr = next((a for a in obj.__mapper__.column_attrs if a.columns[0] is col), None)
        name = attr.key if attr is not None else key
        if name in exclude:
            continue
        out[name.rstrip("_")] = to_json(getattr(obj, name))
    return out


def get_or_404(db: Session, model, id_: Any, what: str):
    try:
        uid = uuid.UUID(str(id_))
    except ValueError:
        raise not_found(what) from None
    obj = db.get(model, uid)
    if obj is None:
        raise not_found(what)
    return obj
