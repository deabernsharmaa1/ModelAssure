"""Isolated test worker: claims jobs from the PostgreSQL queue and executes validation runs.

Run:  python -m workers.executor.main            (PYTHONPATH must include apps/api)
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (str(ROOT / "apps/api"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from db.models import TestRun  # noqa: E402
from db.session import new_session  # noqa: E402
from modules.runner import queue  # noqa: E402
from modules.runner.executor import execute_run  # noqa: E402
from settings import get_settings  # noqa: E402

log = logging.getLogger("modelassure.worker")
_stop = threading.Event()


def _heartbeat_loop(job_id, worker_id: str, lease: int, done: threading.Event) -> None:
    while not done.wait(max(1.0, lease / 3)):
        with new_session() as db:
            queue.heartbeat(db, job_id, worker_id, lease)


def process(job: dict, worker_id: str) -> None:
    s = get_settings()
    done = threading.Event()
    hb = threading.Thread(target=_heartbeat_loop, args=(job["id"], worker_id, s.worker_lease_seconds, done), daemon=True)
    hb.start()
    try:
        if job["kind"] == "execute_run":
            status = execute_run(job["payload"]["run_id"], str(job["organization_id"]))
            log.info("run %s finished with status %s", job["payload"]["run_id"], status)
        else:
            raise ValueError(f"unknown job kind {job['kind']}")
        with new_session() as db:
            queue.finish(db, job["id"], "DONE")
    except Exception as e:  # noqa: BLE001 - the worker must survive any job failure
        err = f"{type(e).__name__}: {e}"
        log.error("job %s failed: %s\n%s", job["id"], err, traceback.format_exc())
        with new_session() as db:
            if job["attempts"] < job["max_attempts"]:
                queue.release_for_retry(db, job["id"], err)
            else:
                queue.finish(db, job["id"], "FAILED", err)
                _mark_run_failed(job, err)
    finally:
        done.set()


def _mark_run_failed(job: dict, err: str) -> None:
    import uuid
    from datetime import datetime, timezone

    from modules.audit.service import record

    if job["kind"] != "execute_run":
        return
    with new_session(job["organization_id"]) as db:
        run = db.get(TestRun, uuid.UUID(job["payload"]["run_id"]))
        if run and run.status not in ("COMPLETED", "CANCELLED"):
            run.status, run.failure_reason, run.completed_at = "FAILED", err[:2000], datetime.now(timezone.utc)
            record(db, org_id=run.organization_id, actor_id=None, actor_label="system:worker", action="run.failed",
                   resource_type="test_run", resource_id=run.id, details={"reason": err[:500]}, outcome="FAILURE")
            db.commit()


def run_once(worker_id: str | None = None) -> bool:
    """Claim and process one job. Returns False when the queue is empty."""
    worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
    with new_session() as db:
        job = queue.claim(db, worker_id, get_settings().worker_lease_seconds)
    if job is None:
        return False
    process(job, worker_id)
    return True


def drain(max_jobs: int = 1000) -> int:
    n = 0
    while n < max_jobs and run_once():
        n += 1
    return n


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    signal.signal(signal.SIGTERM, lambda *_: _stop.set())
    signal.signal(signal.SIGINT, lambda *_: _stop.set())
    log.info("worker %s started", worker_id)
    poll = get_settings().worker_poll_seconds
    while not _stop.is_set():
        try:
            if not run_once(worker_id):
                _stop.wait(poll)
        except Exception:  # noqa: BLE001
            log.exception("worker loop error")
            time.sleep(poll)
    log.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
