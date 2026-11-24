"""Retention: delete raw evidence (requests/responses) older than the institution's retention period
(spec §12.2 "Limit retention of raw application records" / "configurable data deletion").

Outcomes, decisions, rule results and evidence HASHES stay in the database, so reports and the audit
trail still prove what was observed; only the raw payload blobs are removed. Every purge is
recorded in the organisation's audit chain. Runs as the database owner (operations task).

    python scripts/purge_evidence.py --org <organization-id> --older-than-days 365 [--dry-run]
"""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "apps/api"), str(ROOT)]

from sqlalchemy import select  # noqa: E402

from common.storage import get_storage  # noqa: E402
from db.models import TestExecution, TestRun  # noqa: E402
from db.session import new_session  # noqa: E402
from modules.audit.service import record  # noqa: E402
from settings import get_settings  # noqa: E402


def purge(org_id: str, older_than_days: int, dry_run: bool = False, url: str | None = None) -> dict:
    if older_than_days < 1:
        raise SystemExit("--older-than-days must be at least 1")
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    storage = get_storage()
    deleted, runs = 0, set()
    with new_session(uuid.UUID(org_id), url=url or get_settings().migration_database_url) as db:
        q = (select(TestExecution.evidence_ref, TestExecution.run_id).join(TestRun, TestRun.id == TestExecution.run_id)
             .where(TestRun.organization_id == uuid.UUID(org_id), TestRun.completed_at < cutoff))
        for ref, run_id in db.execute(q):
            runs.add(str(run_id))
            if not dry_run:
                storage.delete(ref)
            deleted += 1
        summary = {"cutoff": cutoff.isoformat(), "evidence_objects": deleted, "runs": len(runs), "dry_run": dry_run}
        if not dry_run and deleted:
            record(db, org_id=org_id, actor_id=None, actor_label="system:retention", action="evidence.purge",
                   resource_type="organization", resource_id=org_id, details={**summary, "run_ids": sorted(runs)[:500]})
            db.commit()
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", required=True)
    ap.add_argument("--older-than-days", type=int, required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(purge(a.org, a.older_than_days, a.dry_run))
