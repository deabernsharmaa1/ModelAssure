"""Executes a validation run (Module E, spec §8.1).

For each stored test case: construct the versioned input → call baseline → call candidate →
validate both responses → apply the approved policy assertions → compare → store the outcome and
its evidence. Each execution is committed on its own, so a crash loses nothing and a restarted
worker resumes where the last one stopped (NFR-04). Results never silently become passes.
"""
from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from common.hashing import content_hash
from common.storage import put_json
from common.telemetry import current_traceparent, tracer
from db.models import Dataset, TestCase, TestExecution, TestRun
from db.session import new_session
from modules.audit.service import record
from modules.connectors.client import DecisionClient
from modules.connectors.egress import EgressError, check_url
from modules.connectors.secrets import resolve_secret
from modules.evaluation.engine import INCONCLUSIVE, CaseResult, Observation, evaluate_case
from modules.policies.policy_set import compile_policy_set
from modules.scenarios.generators import DEPENDENCIES, application_id

log = logging.getLogger("modelassure.runner")

INPUT_SCHEMA_VERSION = "canonical-decision-input/1"
OUTPUT_SCHEMA_VERSION = "normalized-decision/1"
PROGRESS_EVERY = 25


class RunAborted(Exception):
    pass


class CaseData:
    """Plain, thread-safe copy of a stored test case (ORM objects never cross threads)."""
    __slots__ = ("id", "case_key", "category", "input", "input_hash", "scenario", "expected_behavior", "skip_reason")

    def __init__(self, c: TestCase):
        self.id, self.case_key, self.category = c.id, c.case_key, c.category
        self.input, self.input_hash, self.scenario = c.input, c.input_hash, c.scenario or {}
        self.expected_behavior, self.skip_reason = c.expected_behavior or {}, c.skip_reason


def _faults(case) -> dict[str, str]:
    return {k: v for k, v in ((case.scenario or {}).get("faults") or {}).items() if v and v != "NONE"}


def _register_faults(snapshot_conn: dict[str, Any], cases: list[CaseData]) -> str | None:
    """Tell the dependency simulator which faults to inject for which application ids.
    Returns an error message if fault injection is unavailable."""
    url = snapshot_conn.get("dependency_control_url")
    needing = [c for c in cases if _faults(c)]
    if not needing:
        return None
    if not url:
        return "no dependency simulator is configured for this connection"
    try:
        key = resolve_secret(snapshot_conn.get("dependency_secret_ref")) or ""
        check_url(url)
        for i in range(0, len(needing), 500):
            batch = [{"key": application_id({"input": c.input}), "faults": _faults(c),
                      "data": (c.scenario or {}).get("dependency_data") or {}} for c in needing[i:i + 500]]
            r = httpx.put(f"{url.rstrip('/')}/control/scenarios", json={"scenarios": batch},
                          headers={"X-Control-Key": key}, timeout=10, follow_redirects=False)
            if r.status_code != 200:
                return f"dependency simulator rejected fault registration (HTTP {r.status_code})"
    except (httpx.HTTPError, EgressError, ValueError) as e:
        return f"dependency simulator unavailable: {e}"
    return None


def _client(snap: dict[str, Any], db: Session) -> DecisionClient:
    recorded = None
    if snap["kind"] == "RECORDED":
        from modules.scenarios.router import load_dataset_rows

        ds = db.get(Dataset, uuid.UUID(snap["recorded_dataset_id"]))
        recorded = {r["case_id"]: r["response"] for r in load_dataset_rows(ds)}
    else:
        check_url(snap["endpoint"])  # re-resolve before every run (DNS rebinding)
    return DecisionClient(snap, resolve_secret(snap.get("secret_ref")), recorded)


def execute_run(run_id: str, org_id: str, heartbeat: Callable[[], None] | None = None) -> str:
    db = new_session(org_id)
    try:
        run = db.get(TestRun, uuid.UUID(run_id))
        if run is None:
            raise RunAborted(f"run {run_id} not found")
        if run.status in ("COMPLETED", "CANCELLED", "FAILED"):
            return run.status
        snap = run.config_snapshot
        run.status = "RUNNING"
        run.started_at = run.started_at or datetime.now(timezone.utc)
        db.commit()

        cand_ps = compile_policy_set(snap["policy_sets"]["candidate"]["content"])
        base_ps = compile_policy_set(snap["policy_sets"]["baseline"]["content"])

        q = select(TestCase).where(TestCase.suite_id == run.suite_id).order_by(TestCase.seq)
        if run.case_filter:
            q = q.where(TestCase.id.in_([uuid.UUID(x) for x in run.case_filter]))
        cases = list(db.scalars(q))
        done = set(db.scalars(select(TestExecution.case_id).where(TestExecution.run_id == run.id)))
        pending = [CaseData(c) for c in cases if c.id not in done]
        total = len(cases)

        try:
            bclient = _client(snap["connections"]["baseline"], db)
            cclient = _client(snap["connections"]["candidate"], db)
        except (EgressError, ValueError) as e:
            return _fail(db, run, f"connection not permitted or unavailable: {e}")

        fault_error = None
        seen_urls = set()
        for side in ("baseline", "candidate"):
            conn = snap["connections"][side]
            if conn.get("dependency_control_url") in seen_urls:
                continue
            seen_urls.add(conn.get("dependency_control_url"))
            fault_error = fault_error or _register_faults(conn, pending)

        excluded = set((snap.get("exclusions") or {}).get("categories") or [])
        exclusion_reason = (snap.get("exclusions") or {}).get("reason") or "category excluded from this run"
        workers = max(1, min(int(snap["connections"]["baseline"].get("max_concurrency") or 1),
                             int(snap["connections"]["candidate"].get("max_concurrency") or 1)))
        t = tracer()
        run_id_str = str(run.id)

        def run_case(case: CaseData):
            with t.start_as_current_span("modelassure.case", attributes={
                    "modelassure.run_id": run_id_str, "modelassure.case_key": case.case_key,
                    "modelassure.category": case.category}):
                tp, trace_id = current_traceparent()
                app_id = application_id({"input": case.input})
                skip = exclusion_reason if case.category in excluded else case.skip_reason
                if skip:
                    return case, None, None, evaluate_case(
                        case_key=case.case_key, category=case.category, case_input=case.input, scenario=case.scenario,
                        expected_behavior=case.expected_behavior, baseline=None, candidate=None,
                        candidate_policies=cand_ps, baseline_policies=base_ps, skip_reason=skip), trace_id
                if fault_error and _faults(case):
                    res = CaseResult(state=INCONCLUSIVE, category=case.category, baseline_decision=None,
                                     candidate_decision=None, changed=False,
                                     inconclusive_reasons=[f"fault injection unavailable: {fault_error}"])
                    return case, None, None, res, trace_id
                repeat = int((case.scenario or {}).get("repeat") or 1)
                b = bclient.call(case.input, tp, case_id=app_id)
                c = cclient.call(case.input, tp, case_id=app_id)
                for _ in range(repeat - 1):
                    b.repeats.append(bclient.call(case.input, tp, case_id=app_id))
                    c.repeats.append(cclient.call(case.input, tp, case_id=app_id))
                res = evaluate_case(case_key=case.case_key, category=case.category, case_input=case.input,
                                    scenario=case.scenario, expected_behavior=case.expected_behavior,
                                    baseline=b, candidate=c, candidate_policies=cand_ps, baseline_policies=base_ps)
                return case, b, c, res, trace_id

        completed = len(done)
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ma-case") as pool:
                futures = [pool.submit(run_case, c) for c in pending]
                for i, fut in enumerate(as_completed(futures), 1):
                    case, b, c, res, trace_id = fut.result()
                    _persist(db, run, case, b, c, res, trace_id)
                    completed += 1
                    if i % PROGRESS_EVERY == 0 or completed == total:
                        db.refresh(run)
                        if run.status == "CANCELLING":
                            for f in futures:
                                f.cancel()
                            run.status = "CANCELLED"
                            run.completed_at = datetime.now(timezone.utc)
                            _progress(run, completed, total)
                            db.commit()
                            return "CANCELLED"
                        _progress(run, completed, total)
                        db.commit()
                        if heartbeat:
                            heartbeat()
        finally:
            bclient.close()
            cclient.close()

        from modules.runner.finalize import finalize_run

        finalize_run(db, run)
        db.commit()
        return run.status
    finally:
        db.close()


def _progress(run: TestRun, completed: int, total: int) -> None:
    run.progress = {"completed": completed, "total": total,
                    "percent": round(100 * completed / total, 1) if total else 100.0}


def _fail(db: Session, run: TestRun, reason: str) -> str:
    run.status, run.failure_reason, run.completed_at = "FAILED", reason, datetime.now(timezone.utc)
    record(db, org_id=run.organization_id, actor_id=None, actor_label="system:worker", action="run.failed",
           resource_type="test_run", resource_id=run.id, details={"reason": reason}, outcome="FAILURE")
    db.commit()
    return "FAILED"


def _persist(db: Session, run: TestRun, case: CaseData, b: Observation | None, c: Observation | None,
             res: CaseResult, trace_id: str | None) -> None:
    now = datetime.now(timezone.utc)
    evidence = {
        "run_id": str(run.id), "run_number": run.run_number, "snapshot_hash": run.snapshot_hash,
        "case_id": str(case.id), "case_key": case.case_key, "category": case.category,
        "input": case.input, "input_hash": case.input_hash, "scenario": case.scenario,
        "expected_behavior": case.expected_behavior,
        "schema_versions": {"input": INPUT_SCHEMA_VERSION, "output": OUTPUT_SCHEMA_VERSION},
        "baseline": b.as_evidence() if b else None, "candidate": c.as_evidence() if c else None,
        "evaluation": res.as_dict(), "trace_id": trace_id, "executed_at": now.isoformat(),
    }
    ref, digest, _ = put_json(f"{run.organization_id}/runs/{run.id}/evidence/{case.case_key}.json", evidence)
    faults = (case.scenario or {}).get("faults") or {}
    fault_label = ",".join(f"{k}={v}" for k, v in sorted(faults.items()) if v != "NONE") or None
    detail = res.as_dict()
    detail["requested_amount"] = case.input.get("requested_amount") if isinstance(case.input, dict) else None
    detail["scenario_faults"] = fault_label
    detail["reason_codes"] = {
        "baseline": (b.normalized or {}).get("reason_codes") if b else None,
        "candidate": (c.normalized or {}).get("reason_codes") if c else None,
    }
    detail["output_hashes"] = {
        "baseline": content_hash(b.normalized) if b and b.normalized else None,
        "candidate": content_hash(c.normalized) if c and c.normalized else None,
    }
    ex = TestExecution(
        id=uuid.uuid4(), organization_id=run.organization_id, run_id=run.id, case_id=case.id, case_key=case.case_key,
        category=case.category, result=res.state, baseline_decision=res.baseline_decision,
        candidate_decision=res.candidate_decision, changed=res.changed, change_class=res.change_class,
        failure_types=res.failure_types, violated_rules=sorted({v["policy_id"] for v in res.violations if v.get("policy_id")} |
                                                             {v["failure_type"] for v in res.violations if not v.get("policy_id")}),
        error_type=(res.error or {}).get("type"), detail=detail, evidence_ref=ref, evidence_hash=digest, trace_id=trace_id,
        observed_versions={"baseline": (b.normalized or {}).get("model_version") if b else None,
                           "candidate": (c.normalized or {}).get("model_version") if c else None},
        duration_ms=(c.duration_ms if c else None),
    )
    ex.detail["duration_baseline_ms"] = b.duration_ms if b else None
    db.add(ex)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # already recorded by a previous attempt of this run
