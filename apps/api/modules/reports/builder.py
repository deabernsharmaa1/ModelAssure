"""Versioned release-assurance report (Module G).

Sections: executive summary, methodology, coverage, decision changes, policy violations,
financial implications, limitations, remediation, review.

`results_digest` is a hash over every execution's outcome and evidence hash. Because executions
are append-only, regenerating a report always yields the same digest for the same run (QA-11);
only the report version, generation time and live finding/review status can differ.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.hashing import content_hash
from db.models import Finding, Review, TestExecution, TestRun, User
from modules.evaluation.assessment import refresh_assessment

REPORT_SCHEMA = "modelassure-release-report/1"


def results_digest(db: Session, run: TestRun) -> str:
    rows = db.execute(select(TestExecution.case_key, TestExecution.result, TestExecution.baseline_decision,
                             TestExecution.candidate_decision, TestExecution.change_class, TestExecution.evidence_hash)
                      .where(TestExecution.run_id == run.id).order_by(TestExecution.case_key)).all()
    return content_hash([list(r) for r in rows])


def build_report(db: Session, run: TestRun, version: int, generated_by: str) -> dict[str, Any]:
    snap = run.config_snapshot
    s = run.summary or {}
    assessment = refresh_assessment(db, run)
    findings = list(db.scalars(select(Finding).where(Finding.run_id == run.id).order_by(Finding.finding_number)))
    reviews = list(db.scalars(select(Review).where(Review.run_id == run.id).order_by(Review.created_at)))
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_([r.reviewer_id for r in reviews] +
                                                                         [f.owner_id for f in findings if f.owner_id])))}
    retest_runs = list(db.scalars(select(TestRun).where(TestRun.parent_run_id == run.id).order_by(TestRun.run_number)))
    crit = [f for f in findings if f.severity == "CRITICAL"]
    by = s.get("by_result", {})

    violation_rows = []
    for f in findings:
        if f.type in ("POLICY_VIOLATION", "CHANGE_CONTROL_VIOLATION") or f.type.endswith("ACCEPTED") or f.type in (
                "UNSAFE_ERROR_HANDLING", "INCONSISTENT_DUPLICATE", "EXPECTATION_MISMATCH"):
            violation_rows.append({
                "finding": f"F-{f.finding_number}", "title": f.title, "rules": f.rule_ids, "severity": f.severity,
                "status": f.status, "affected_cases": f.affected_count,
                "expected": f.summary.get("expected"), "observed": f.summary.get("candidate_decisions"),
                "reproducible_cases": f.summary.get("affected_case_keys", [])[:10],
                "suspected_cause": f.suspected_cause,
            })

    ps_c, ps_b = snap["policy_sets"]["candidate"], snap["policy_sets"]["baseline"]
    expo = s.get("exposure", {})
    limitations = [
        "Test inputs are synthetic (fictional applicants) unless a historical replay dataset is listed in the methodology.",
        "No labelled outcome data was supplied, so predictive accuracy, calibration, fairness and drift were not assessed (Phase 2 capabilities).",
        "Failure counts come from designed scenarios; they are not estimates of real-world frequency or credit loss.",
        "Only behaviour observable through the decision API was tested; internal model logic was not inspected.",
        f"Results apply to the tested sandbox ({snap['project'].get('name')}) and the exact versions and configuration recorded below.",
    ]
    if by.get("ERROR"):
        limitations.append(f"{by['ERROR']} case(s) ended in technical errors and provide no evidence about decision behaviour.")
    if by.get("INCONCLUSIVE"):
        limitations.append(f"{by['INCONCLUSIVE']} case(s) were inconclusive.")
    if by.get("SKIPPED"):
        limitations.append(f"{by['SKIPPED']} case(s) were skipped: {(snap.get('exclusions') or {}).get('reason')}.")
    if run.scope != "FULL":
        limitations.append("This report covers a partial retest, not the full suite.")

    return {
        "schema": REPORT_SCHEMA,
        "report_version": version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": generated_by,
        "run": {"id": str(run.id), "run_number": run.run_number, "scope": run.scope, "status": run.status,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                "snapshot_hash": run.snapshot_hash, "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None},
        "results_digest": results_digest(db, run),
        "executive_summary": {
            "project": snap["project"]["name"], "risk_classification": snap["project"]["risk_classification"],
            "baseline_version": snap["versions"]["baseline"]["version"], "candidate_version": snap["versions"]["candidate"]["version"],
            "cases_executed": s.get("total", 0), "results": by,
            "critical_findings": [{"finding": f"F-{f.finding_number}", "title": f.title, "status": f.status,
                                   "affected_cases": f.affected_count} for f in crit],
            "assessment": assessment.get("outcome"), "recommendation": assessment.get("recommendation"),
            "disclaimer": assessment.get("disclaimer"),
        },
        "methodology": {
            "suite": snap["suite"], "scenario_design": (
                "Deterministic fixtures, threshold boundary values taken from the approved policy parameters, missing-data "
                "variants, dependency faults injected through a controlled dependency simulator, credit/fraud conflicts, "
                "duplicate requests, invalid inputs, and property-based regression cases generated with a fixed seed. "
                "Cases were generated once and stored; the run replayed the stored cases."),
            "policy_sets": {
                "candidate": {k: ps_c[k] for k in ("name", "version", "label", "approval_status", "content_hash")},
                "baseline": {k: ps_b[k] for k in ("name", "version", "label", "approval_status", "content_hash")},
            },
            "execution_environment": {
                side: {"endpoint": c["endpoint"], "version": c["version"], "kind": c["kind"], "mapping_hash": c["mapping_hash"],
                       "timeout_ms": c["timeout_ms"], "max_concurrency": c["max_concurrency"],
                       "expected_model_version": c.get("expected_model_version")}
                for side, c in snap["connections"].items()},
            "mode": run.mode, "platform_version": snap.get("platform_version"),
            "result_states": "PASS, FAIL, CHANGED (EXPECTED/UNEXPECTED), ERROR, INCONCLUSIVE, SKIPPED. Technical errors are never counted as passes.",
        },
        "coverage": {"by_category": s.get("coverage", []), "by_result": by, "error_rate": s.get("error_rate"),
                     "inconclusive_rate": s.get("inconclusive_rate"), "errors_by_type": s.get("errors_by_type", {})},
        "decision_changes": {
            "agreement_rate": s.get("decision_agreement_rate"), "agreement_note": s.get("agreement_note"),
            "transition_matrix": s.get("transition_matrix", {}), "transitions": s.get("transitions", {}),
            "classification": s.get("changes", {}),
            "unexpected_change_groups": [{"finding": f"F-{f.finding_number}", "title": f.title, "cases": f.affected_count,
                                          "status": f.status} for f in findings if f.type == "UNEXPECTED_CHANGE"],
            "dependency_behaviour": s.get("dependency_behaviour", []),
        },
        "policy_violations": violation_rows,
        "financial_implications": {
            "assessed": False,
            "statement": ("Not assessed: no validated probability-of-default and loss-given-default assumptions or labelled "
                          "outcomes were supplied. Expected-loss estimates would require them (spec §8.4)."),
            "exposure_indicators": expo,
        },
        "limitations": limitations,
        "remediation": [{
            "finding": f"F-{f.finding_number}", "title": f.title, "type": f.type, "severity": f.severity, "status": f.status,
            "owner": users[f.owner_id].email if f.owner_id in users else None, "suspected_cause": f.suspected_cause,
            "retest_history": f.retest_history, "risk_acceptance": f.risk_acceptance, "closure": f.closure,
        } for f in findings],
        "retest_runs": [{"run_number": r.run_number, "scope": r.scope, "status": r.status,
                         "candidate": r.config_snapshot["versions"]["candidate"]["version"],
                         "assessment": (r.assessment or {}).get("outcome")} for r in retest_runs],
        "regulatory_mapping": _mapping_summary(db, run),
        "review": {
            "assessment": assessment,
            "decisions": [{"decision": r.decision, "rationale": r.rationale, "at": r.created_at.isoformat(),
                           "reviewer": users[r.reviewer_id].email if r.reviewer_id in users else None,
                           "assessment_at_review": r.assessment_snapshot.get("outcome")} for r in reviews],
            "authority": "The institution retains final release authority. ModelAssure is not a regulator and does not authorise deployment.",
        },
    }


def next_report_version(db: Session, run: TestRun) -> int:
    from db.models import Artifact

    return (db.scalar(select(func.max(Artifact.version)).where(Artifact.run_id == run.id, Artifact.type == "REPORT_JSON")) or 0) + 1


def _mapping_summary(db: Session, run: TestRun) -> dict[str, Any]:
    from modules.compliance.mapping import evaluate

    m = evaluate(db, run)
    return {"disclaimer": m["disclaimer"], "frameworks": [{
        "framework_id": f["framework_id"], "name": f["name"], "issuer": f["issuer"], "status": f["status"],
        "nature": f["nature"], "reference_note": f.get("reference_note"), "summary": f["summary"],
        "controls": [{"control_id": c["control_id"], "reference": c["reference"], "topic": c["topic"], "status": c["status"],
                      "evidence": [x["detail"] for x in c["checks"]], "note": c.get("note")} for c in f["controls"]],
    } for f in m["frameworks"]]}
