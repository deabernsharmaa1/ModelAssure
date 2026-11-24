"""Database sessions bound to a tenant.

Every transaction opened by a tenant session sets the PostgreSQL settings `app.current_org` and
`app.current_user` (transaction-local), which the row-level-security policies use. A session
without an org can see no tenant data at all.
"""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from settings import get_settings

_engines: dict[str, Engine] = {}


def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    if url not in _engines:
        _engines[url] = create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)
    return _engines[url]


SessionLocal = sessionmaker(expire_on_commit=False)


@event.listens_for(Session, "after_begin")
def _apply_tenant(session: Session, transaction, connection) -> None:  # noqa: ANN001
    org = session.info.get("org_id")
    user = session.info.get("user_id")
    connection.exec_driver_sql(
        "SELECT set_config('app.current_org', %s, true), set_config('app.current_user', %s, true)",
        (str(org) if org else "", str(user) if user else ""),
    )


def new_session(org_id: uuid.UUID | str | None = None, user_id: uuid.UUID | str | None = None,
                url: str | None = None) -> Session:
    s = SessionLocal(bind=get_engine(url))
    s.info["org_id"] = str(org_id) if org_id else None
    s.info["user_id"] = str(user_id) if user_id else None
    return s


@contextmanager
def tenant_session(org_id, user_id=None, url: str | None = None) -> Iterator[Session]:
    s = new_session(org_id, user_id, url)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
