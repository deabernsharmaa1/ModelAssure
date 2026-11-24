"""Group related failures into findings (Module F) and apply retest outcomes.

Grouping key ("signature"):
  FAIL           sorted set of violated rules            → POLICY_VIOLATION / CHANGE_CONTROL_VIOLATION / ...
  ERROR          error type                              → TECHNICAL_ERROR
  INCONCLUSIVE   first reason (normalised)               → INCONCLUSIVE
  CHANGED/UNEXP. baseline→candidate transition           → UNEXPECTED_CHANGE

The suspected cause is only stated when the evidence supports it: e.g. a reason code present on
every affected candidate response and absent from every baseline response.
"""
from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from db.models import Finding, FindingExecution, TestExecution, TestRun
from modules.policies.dsl import SEVERITIES
from modules.policies.policy_set import compile_policy_set

FAILING_TYPES = {"POLICY_VIOLATION", "CHANGE_CONTROL_VIOLATION", "INVALID_INPUT_ACCEPTED", "UNSAFE_ERROR_HANDLING",
                 "INCONSISTENT_DUPLICATE", "EXPECTATION_MISMATCH"}
UNRESOLVED = {"OPEN", "INVESTIGATING", "FIX_PROPOSED", "READY_FOR_RETEST"}
SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}  # CRITICAL=0 (most severe)


def _max_sev(sevs) -> str:
    sevs = [s for s in sevs if s in SEV_RANK]
    return min(sevs, key=SEV_RANK.get) if sevs else "MEDIUM"


def _suspected_cause(execs: list[TestExecution]) -> str | None:
    cand = [set((e.detail.get("reason_codes") or {}).get("candidate") or []) for e in execs]
    base = [set((e.detail.get("reason_codes") or {}).get("baseline") or []) for e in execs]
    if cand and all(cand):
        common = set.intersection(*cand) - set.union(*base) if base else set.intersection(*cand)
        if common:
            return (f"Every affected candidate response carries reason code(s) {', '.join(sorted(common))}, "
                    f"which never appear in the baseline responses for the same cases.")
    faults = Counter(e.detail.get("scenario_faults") for e in execs if e.detail.get("scenario_faults"))
    if faults and sum(faults.values()) == len(execs) and len(faults) <= 3:
        return f"All affected cases involve injected dependency faults: {', '.join(sorted(faults))}."
    return None


def group_findings(db: Session, run: TestRun) -> list[Finding]:
    snap = run.config_snapshot
    cps = compile_policy_set(snap["policy_sets"]["candidate"]["content"])
    execs = list(db.scalars(select(TestExecution).where(TestExecution.run_id == run.id)))
    groups: dict[tuple[str, str], list[TestExecution]] = defaultdict(list)
    for e in execs:
        if e.result == "FAIL":
            ftype = next((t for t in ("POLICY_VIOLATION", "CHANGE_CONTROL_VIOLATION") if t in e.failure_types),
                         (e.failure_types or ["POLICY_VIOLATION"])[0])
            groups[(ftype, "+".join(sorted(e.violated_rules)) or ftype)].append(e)
        elif e.result == "ERROR":
            groups[("TECHNICAL_ERROR", e.error_type or "ERROR")].append(e)
        elif e.result == "INCONCLUSIVE":
            reason = (e.detail.get("inconclusive_reasons") or ["unknown"])[0]
            groups[("INCONCLUSIVE", re.sub(r"\(.*?\)", "", reason.split(":")[0]).strip()[:120])].append(e)
        elif e.result == "CHANGED" and e.change_class == "UNEXPECTED":
            groups[("UNEXPECTED_CHANGE", f"{e.baseline_decision}->{e.candidate_decision}")].append(e)

    # In a retest, failures matching a retested finding are tracked on the original finding.
    retest_sigs = set()
    if run.retest_finding_ids:
        for f in db.scalars(select(Finding).where(Finding.id.in_([uuid.UUID(x) for x in run.retest_finding_ids]))):
            retest_sigs.add((f.type, f.signature))

    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"finding:{run.organization_id}"})
    next_no = (db.scalar(select(func.max(Finding.finding_number)).where(Finding.organization_id == run.organization_id)) or 0) + 1
    created: list[Finding] = []
    order = {"POLICY_VIOLATION": 0, "CHANGE_CONTROL_VIOLATION": 1, "TECHNICAL_ERROR": 3, "INCONCLUSIVE": 4, "UNEXPECTED_CHANGE": 5}
    for (ftype, sig), items in sorted(groups.items(), key=lambda kv: (order.get(kv[0][0], 2), -len(kv[1]))):
        if (ftype, sig) in retest_sigs:
            continue
        severity, title, rules = _describe(ftype, sig, items, cps)
        f = Finding(
            id=uuid.uuid4(), organization_id=run.organization_id, finding_number=next_no, run_id=run.id,
            project_id=run.project_id, type=ftype, signature=sig, title=title, rule_ids=rules,
            severity=severity, suggested_severity=severity, status="OPEN", affected_count=len(items),
            summary=_summary(items, snap), suspected_cause=_suspected_cause(items),
            history=[{"at": datetime.now(timezone.utc).isoformat(), "by": "system", "event": "created", "status": "OPEN"}],
        )
        next_no += 1
        db.add(f)
        db.flush()
        db.bulk_insert_mappings(FindingExecution, [{"finding_id": f.id, "execution_id": e.id,
                                                    "organization_id": run.organization_id} for e in items])
        created.append(f)
    if run.retest_finding_ids:
        apply_retest(db, run, execs)
    return created


def _describe(ftype: str, sig: str, items: list[TestExecution], cps) -> tuple[str, str, list[str]]:
    sev_rules = cps.severity_rules
    if ftype in ("POLICY_VIOLATION", "CHANGE_CONTROL_VIOLATION") or ftype in FAILING_TYPES:
        rules = sig.split("+") if sig else []
        sevs = [v.get("severity") for e in items for v in e.detail.get("violations", [])]
        primary = None
        for r in rules:
            p = cps.by_id(r)
            if p and (primary is None or SEV_RANK[p.severity] < SEV_RANK[primary.severity]):
                primary = p
        if primary is not None:
            title = primary.description or primary.policy_id
        else:
            cc = next((c for c in cps.change_controls if c.rule_id in rules), None)
            title = cc.description if cc else {"INVALID_INPUT_ACCEPTED": "Invalid input accepted instead of rejected",
                                               "UNSAFE_ERROR_HANDLING": "Invalid input caused a server error",
                                               "INCONSISTENT_DUPLICATE": "Repeated identical requests received different decisions",
                                               "EXPECTATION_MISMATCH": "Decision differs from the case's expected outcome"}.get(sig, sig)
        return _max_sev(sevs), f"{title} — violated: {', '.join(rules)}", rules
    if ftype == "TECHNICAL_ERROR":
        return sev_rules["TECHNICAL_ERROR"], f"Technical execution errors ({sig})", []
    if ftype == "INCONCLUSIVE":
        return sev_rules["INCONCLUSIVE"], f"Inconclusive results: {sig}", []
    flags = [fl for e in items for fl in e.detail.get("change_flags", [])]
    sev = _max_sev([fl["severity"] for fl in flags]) if flags else sev_rules["UNEXPECTED_CHANGE"]
    rules = sorted({fl["rule_id"] for fl in flags})
    frm, to = sig.split("->")
    return sev, f"Unexpected decision changes {frm} → {to}" + (f" (flagged by {', '.join(rules)})" if rules else ""), rules


def _summary(items: list[TestExecution], snap: dict) -> dict[str, Any]:
    first = items[0]
    expected = Counter(str(v.get("expected")) for e in items for v in e.detail.get("violations", []))
    return {
        "affected_case_keys": [e.case_key for e in items[:200]],
        "sample_execution_ids": [str(e.id) for e in items[:10]],
        "categories": dict(Counter(e.category for e in items)),
        "baseline_decisions": dict(Counter(str(e.baseline_decision) for e in items)),
        "candidate_decisions": dict(Counter(str(e.candidate_decision) for e in items)),
        "expected": dict(expected),
        "error_types": dict(Counter(e.error_type for e in items if e.error_type)),
        "first_reason": (first.detail.get("inconclusive_reasons") or [None])[0],
        "versions": {"baseline": snap["versions"]["baseline"]["version"], "candidate": snap["versions"]["candidate"]["version"]},
        "exposure_requested_amount": float(sum((e.detail.get("requested_amount") or 0) for e in items
                                               if isinstance(e.detail.get("requested_amount"), (int, float)))),
    }


def apply_retest(db: Session, run: TestRun, execs: list[TestExecution]) -> None:
    """Record the retest outcome on each retested finding. The original run and its evidence are
    never modified (QA-10)."""
    by_case = {e.case_id: e for e in execs}
    now = datetime.now(timezone.utc).isoformat()
    for f in db.scalars(select(Finding).where(Finding.id.in_([uuid.UUID(x) for x in run.retest_finding_ids]))):
        orig_cases = list(db.scalars(select(TestExecution.case_id).join(FindingExecution, FindingExecution.execution_id == TestExecution.id)
                                     .where(FindingExecution.finding_id == f.id)))
        outcomes = Counter()
        for cid in orig_cases:
            e = by_case.get(cid)
            if e is None:
                outcomes["NOT_RETESTED"] += 1
            elif e.result == "PASS" or (e.result == "CHANGED" and e.change_class in ("EXPECTED", "UNEXPECTED")):
                outcomes["PASSED"] += 1
            else:
                outcomes[e.result] += 1
        passed = outcomes["PASSED"] == len(orig_cases) and len(orig_cases) > 0
        entry = {"run_id": str(run.id), "run_number": run.run_number, "at": now,
                 "candidate_version": run.config_snapshot["versions"]["candidate"]["version"],
                 "passed": passed, "outcomes": dict(outcomes)}
        f.retest_history = [*f.retest_history, entry]
        new_status = "RESOLVED" if passed else "OPEN"
        f.history = [*f.history, {"at": now, "by": "system", "event": "retest", "run_number": run.run_number,
                                  "from": f.status, "status": new_status}]
        f.status = new_status
        f.updated_at = datetime.now(timezone.utc)
