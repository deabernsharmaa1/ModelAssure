"""Release-gate assessment (spec §13).

Outcome precedence: FAIL > INCONCLUSIVE > PASS.
  * Any unresolved finding of a failing type whose severity is in the institution's
    `fail_on_unresolved_severities` → FAIL. (A known critical failure is never softened to
    "inconclusive" because evidence elsewhere is incomplete.)
  * Otherwise: an incomplete or invalid evidence base → INCONCLUSIVE (unapproved policies,
    partial/retest scope, technical errors or inconclusive cases above the allowed rate, skipped
    mandatory categories, run not completed).
  * Otherwise PASS — meaning the defined tests passed, not that the system is free of risk.
Unexpected but unprohibited decision changes are flagged for review in every outcome.
The assessment is a recommendation to authorised humans; it never authorises deployment.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Finding, TestRun
from modules.findings.grouping import FAILING_TYPES, UNRESOLVED
from modules.policies.policy_set import DEFAULT_RELEASE_CRITERIA

DISCLAIMER = ("A PASS means the defined tests passed under the stated configuration. It is not a statement that the "
              "system is free of risk, and it is not a release authorisation: the institution retains final release authority.")


def assess_run(db: Session, run: TestRun) -> dict[str, Any]:
    snap = run.config_snapshot
    criteria = {**DEFAULT_RELEASE_CRITERIA, **(snap["policy_sets"]["candidate"]["content"].get("release_criteria") or {})}
    fail_on = set(criteria["fail_on_unresolved_severities"])
    summary = run.summary or {}
    findings = list(db.scalars(select(Finding).where(Finding.run_id == run.id)))
    fail_reasons: list[str] = []
    inconclusive_reasons: list[str] = []
    flags: list[str] = []

    this_version = snap["versions"]["candidate"]["version"]
    for f in findings:
        if f.type not in FAILING_TYPES or f.severity not in fail_on:
            continue
        if f.status in UNRESOLVED:
            fail_reasons.append(f"Unresolved {f.severity} finding F-{f.finding_number}: {f.title} ({f.affected_count} cases)")
        elif f.status in ("RESOLVED", "CLOSED") and not (f.closure or {}).get("reason") == "FALSE_POSITIVE":
            # A fix verified on another build does not make THIS build releasable.
            fixed_in = [h["candidate_version"] for h in f.retest_history if h.get("passed")]
            if fixed_in and this_version not in fixed_in:
                fail_reasons.append(f"F-{f.finding_number} was fixed in {fixed_in[-1]}, but this run's candidate "
                                    f"{this_version} still contains it. Assess a full run of {fixed_in[-1]} instead.")

    if run.status != "COMPLETED":
        inconclusive_reasons.append(f"Run is {run.status}; the evidence is incomplete.")
    for side, ps in snap["policy_sets"].items():
        if ps["approval_status"] != "APPROVED":
            inconclusive_reasons.append(f"{side.capitalize()} policy set '{ps['name']}' v{ps['version']} is {ps['approval_status']}, not approved.")
    if run.scope != "FULL":
        inconclusive_reasons.append(f"This is a {run.scope.lower()} run covering {summary.get('total', 0)} of "
                                    f"{snap['suite']['case_count']} suite cases; run the full suite before a release decision.")
    total = summary.get("total") or 0
    by = summary.get("by_result") or {}
    if total:
        if summary.get("error_rate", 0) > float(criteria["max_error_rate"]):
            inconclusive_reasons.append(f"{by.get('ERROR', 0)} case(s) ended in technical errors "
                                        f"(rate {summary['error_rate']:.2%} > allowed {float(criteria['max_error_rate']):.2%}).")
        if summary.get("inconclusive_rate", 0) > float(criteria["max_inconclusive_rate"]):
            inconclusive_reasons.append(f"{by.get('INCONCLUSIVE', 0)} case(s) were inconclusive "
                                        f"(rate {summary['inconclusive_rate']:.2%} > allowed {float(criteria['max_inconclusive_rate']):.2%}).")
    mandatory = set(criteria.get("mandatory_categories") or []) or set((snap["suite"].get("category_counts") or {}).keys())
    covered = {c["category"]: c for c in summary.get("coverage", [])}
    for cat in sorted(mandatory):
        c = covered.get(cat)
        if run.scope == "FULL" and (c is None or c["total"] == 0):
            inconclusive_reasons.append(f"Mandatory category {cat} was not executed.")
        elif c and c.get("skipped"):
            inconclusive_reasons.append(f"{c['skipped']} case(s) in mandatory category {cat} were skipped.")
    if total == 0:
        inconclusive_reasons.append("No test cases were executed.")

    unexpected = [f for f in findings if f.type == "UNEXPECTED_CHANGE" and f.status in UNRESOLVED]
    if unexpected:
        n = sum(f.affected_count for f in unexpected)
        flags.append(f"{n} unexpected but unprohibited decision change(s) in {len(unexpected)} group(s) are flagged for review.")
    other_open = [f for f in findings if f.type in FAILING_TYPES and f.status in UNRESOLVED and f.severity not in fail_on]
    if other_open:
        flags.append(f"{len(other_open)} unresolved lower-severity finding(s) do not block under the configured criteria.")
    accepted = [{"finding": f"F-{f.finding_number}", "title": f.title, "severity": f.severity,
                 "owner": (f.risk_acceptance or {}).get("owner_email"), "rationale": (f.risk_acceptance or {}).get("rationale"),
                 "authorised_by": (f.risk_acceptance or {}).get("authorised_by")}
                for f in findings if f.status == "ACCEPTED_RISK"]
    if accepted:
        flags.append(f"{len(accepted)} finding(s) are covered by documented risk acceptance.")

    outcome = "FAIL" if fail_reasons else "INCONCLUSIVE" if inconclusive_reasons else "PASS"
    recommendation = {
        "FAIL": "Withhold deployment: unresolved findings violate the approved release criteria.",
        "INCONCLUSIVE": "No release recommendation can be made until the evidence gaps are closed.",
        "PASS": "The configured release criteria are met. Release remains subject to authorised human review.",
    }[outcome]
    return {
        "outcome": outcome, "recommendation": recommendation, "fail_reasons": fail_reasons,
        "inconclusive_reasons": inconclusive_reasons, "flags": flags, "accepted_risks": accepted,
        "criteria": criteria, "assessed_at": datetime.now(timezone.utc).isoformat(), "disclaimer": DISCLAIMER,
    }


def refresh_assessment(db: Session, run: TestRun) -> dict[str, Any]:
    if run.status == "COMPLETED":
        run.assessment = assess_run(db, run)
    return run.assessment or {}
