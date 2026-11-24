"""PostgreSQL-backed job queue (replaces Celery + Redis, agreed change #9).

Jobs are claimed with `FOR UPDATE SKIP LOCKED` and leased. A worker that dies stops renewing its
lease; another worker reclaims the job and resumes. Because every completed test execution is
committed individually, no result is lost on a crash (NFR-04).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from db.models import Job


def enqueue(db: Session, org_id, kind: str, payload: dict[str, Any], max_attempts: int = 3) -> Job:
    job = Job(id=uuid.uuid4(), organization_id=org_id, kind=kind, payload=payload, max_attempts=max_attempts)
    db.add(job)
    db.flush()
    return job


CLAIM_SQL = text("""
UPDATE jobs SET status = 'RUNNING', locked_by = :worker, locked_until = now() + make_interval(secs => :lease),
                attempts = attempts + 1, updated_at = now()
WHERE id = (
  SELECT id FROM jobs
  WHERE (status = 'QUEUED') OR (status = 'RUNNING' AND locked_until < now())
  ORDER BY created_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING id, organization_id, kind, payload, attempts, max_attempts
""")


def claim(db: Session, worker_id: str, lease_seconds: int) -> dict[str, Any] | None:
    r = db.execute(CLAIM_SQL, {"worker": worker_id, "lease": lease_seconds}).mappings().first()
    db.commit()
    return dict(r) if r else None


def heartbeat(db: Session, job_id, worker_id: str, lease_seconds: int) -> bool:
    r = db.execute(text("UPDATE jobs SET locked_until = now() + make_interval(secs => :lease), updated_at = now() "
                        "WHERE id = :id AND locked_by = :worker AND status = 'RUNNING' RETURNING id"),
                   {"id": job_id, "worker": worker_id, "lease": lease_seconds}).first()
    db.commit()
    return r is not None


def finish(db: Session, job_id, status: str, error: str | None = None) -> None:
    db.execute(text("UPDATE jobs SET status = :s, last_error = :e, locked_until = NULL, updated_at = now() WHERE id = :id"),
               {"s": status, "e": error, "id": job_id})
    db.commit()


def release_for_retry(db: Session, job_id, error: str) -> None:
    db.execute(text("UPDATE jobs SET status = 'QUEUED', last_error = :e, locked_by = NULL, locked_until = NULL, "
                    "updated_at = now() WHERE id = :id"), {"e": error, "id": job_id})
    db.commit()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def lease_expired(locked_until: datetime | None) -> bool:
    return locked_until is None or locked_until < now_utc() - timedelta(seconds=1)
