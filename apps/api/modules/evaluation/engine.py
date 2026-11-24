"""Case evaluation (Module E, spec §8.1 made production-grade).

Result-state precedence for one case (highest wins):

  SKIPPED       case excluded for a documented reason
  ERROR         the candidate could not be evaluated (timeout, transport/HTTP error, malformed JSON,
                schema error, wrong deployed version). Never counted as a pass (NFR-06, QA-05/06).
  FAIL          the candidate violated an approved assertion, a PROHIBIT change-control rule, or a
                category-specific expectation (invalid input accepted, inconsistent duplicates, ...)
  INCONCLUSIVE  a policy could not be evaluated (missing data, equal-precedence conflict) or the
                baseline was unavailable so the comparison cannot be made
  CHANGED       the decision differs from baseline without a confirmed violation. Classified as
                EXPECTED (explained by an approved change or policy revision) or UNEXPECTED.
                A changed decision that also violates a policy is FAIL with change class VIOLATION:
                an approved change never excuses a violation.
  PASS          executed and satisfied every configured assertion with no decision change
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from modules.policies.dsl import eval_node
from modules.policies.policy_set import (
    SATISFIED,
    UNKNOWN,
    VIOLATED,
    CompiledPolicySet,
    build_context,
    evaluate_policies,
    policy_fingerprint,
)

PASS, FAIL, CHANGED, ERROR, INCONCLUSIVE, SKIPPED = "PASS", "FAIL", "CHANGED", "ERROR", "INCONCLUSIVE", "SKIPPED"
RESULT_STATES = (PASS, FAIL, CHANGED, ERROR, INCONCLUSIVE, SKIPPED)

CATEGORIES = (
    "NORMAL", "BOUNDARY", "MISSING_DATA", "DEPENDENCY_FAILURE", "CROSS_SYSTEM_CONFLICT",
    "VERSION_REGRESSION", "DUPLICATE", "INVALID_INPUT", "HISTORICAL_REPLAY",
)


@dataclass
class Observation:
    """What one system returned for one request."""
    status: str  # OK | REJECTED (4xx) | ERROR
    http_status: int | None = None
    error_type: str | None = None  # TIMEOUT | CONNECTION_ERROR | HTTP_ERROR | MALFORMED_RESPONSE | SCHEMA_ERROR | NO_RECORDED_OUTPUT | VERSION_MISMATCH
    error_detail: str | None = None
    raw_request: Any = None
    raw_response: Any = None
    raw_text: str | None = None
    normalized: dict[str, Any] | None = None
    transformations: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float | None = None
    trace_id: str | None = None
    repeats: list["Observation"] = field(default_factory=list)

    @property
    def decision(self) -> str | None:
        return (self.normalized or {}).get("decision")

    def as_evidence(self) -> dict[str, Any]:
        return {
            "status": self.status, "http_status": self.http_status, "error_type": self.error_type,
            "error_detail": self.error_detail, "raw_request": self.raw_request,
            "raw_response": self.raw_response if self.raw_response is not None else self.raw_text,
            "normalized": self.normalized, "transformations": self.transformations,
            "duration_ms": self.duration_ms, "trace_id": self.trace_id,
            "repeats": [r.as_evidence() for r in self.repeats],
        }


@dataclass
class CaseResult:
    state: str
    category: str
    baseline_decision: str | None
    candidate_decision: str | None
    changed: bool
    change_class: str | None = None  # EXPECTED | UNEXPECTED | PROHIBITED | VIOLATION
    explained_by: list[str] = field(default_factory=list)
    failure_types: list[str] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    policy_outcomes: list[dict[str, Any]] = field(default_factory=list)
    baseline_violations: list[dict[str, Any]] = field(default_factory=list)
    change_flags: list[dict[str, Any]] = field(default_factory=list)
    inconclusive_reasons: list[str] = field(default_factory=list)
    error: dict[str, Any] | None = None
    policy_results: list[dict[str, Any]] = field(default_factory=list)  # spec §7.4 rows

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _side_error(obs: Observation | None) -> dict[str, Any] | None:
    if obs is None:
        return {"type": "NOT_EXECUTED", "detail": "no observation"}
    if obs.status == "ERROR":
        return {"type": obs.error_type or "ERROR", "detail": obs.error_detail, "http_status": obs.http_status}
    if obs.status == "REJECTED":
        # A valid request rejected with 4xx: the system could not be evaluated for this case.
        return {"type": "REQUEST_REJECTED", "detail": obs.error_detail, "http_status": obs.http_status}
    return None


def evaluate_case(
    *,
    case_key: str,
    category: str,
    case_input: Any,
    scenario: dict[str, Any],
    expected_behavior: dict[str, Any] | None,
    baseline: Observation | None,
    candidate: Observation | None,
    candidate_policies: CompiledPolicySet,
    baseline_policies: CompiledPolicySet,
    skip_reason: str | None = None,
) -> CaseResult:
    expected_behavior = expected_behavior or {}
    b_dec = baseline.decision if baseline else None
    c_dec = candidate.decision if candidate else None
    res = CaseResult(state=PASS, category=category, baseline_decision=b_dec, candidate_decision=c_dec,
                     changed=False)

    if skip_reason:
        res.state = SKIPPED
        res.inconclusive_reasons.append(skip_reason)
        return res

    # ---- Invalid-input cases: the expected behaviour is a safe rejection ----------------------
    if category == "INVALID_INPUT" or expected_behavior.get("type") == "REJECT":
        return _evaluate_rejection(res, baseline, candidate, candidate_policies)

    # ---- Technical errors on the candidate --------------------------------------------------
    c_err = _side_error(candidate)
    if c_err:
        res.state = ERROR
        res.error = {"side": "candidate", **c_err}
        res.failure_types.append("TECHNICAL_ERROR")
        return res

    # ---- Duplicate-request consistency ------------------------------------------------------
    if candidate.repeats:
        decisions = {candidate.decision, *[r.decision for r in candidate.repeats]}
        repeat_errors = [r for r in candidate.repeats if r.status != "OK"]
        if repeat_errors:
            res.state = ERROR
            res.error = {"side": "candidate", "type": repeat_errors[0].error_type or "ERROR",
                         "detail": "a repeated request failed: " + (repeat_errors[0].error_detail or "")}
            res.failure_types.append("TECHNICAL_ERROR")
            return res
        if len(decisions) > 1:
            res.failure_types.append("INCONSISTENT_DUPLICATE")
            res.violations.append({
                "policy_id": None, "failure_type": "INCONSISTENT_DUPLICATE",
                "expected": "identical decision for identical requests",
                "observed": sorted(d for d in decisions if d), "severity": candidate_policies.severity_rules["INCONSISTENT_DUPLICATE"],
            })

    # ---- Policy assertions on the candidate ---------------------------------------------------
    b_out = baseline.normalized if baseline and baseline.status == "OK" else None
    ctx = build_context(candidate_policies, case_input, scenario, candidate.normalized, b_out)
    outcomes = evaluate_policies(candidate_policies, ctx)
    res.policy_outcomes = [o.as_dict() for o in outcomes]
    for o in outcomes:
        if o.status == VIOLATED:
            res.failure_types.append("POLICY_VIOLATION")
            res.violations.append({
                "policy_id": o.policy_id, "policy_version": o.version, "failure_type": "POLICY_VIOLATION",
                "expected": o.expected, "observed": o.observed, "severity": o.severity, "reason": o.reason,
            })
        elif o.status == UNKNOWN:
            res.inconclusive_reasons.append(f"{o.policy_id}: {o.reason}")
        if o.status in (SATISFIED, VIOLATED):
            res.policy_results.append({
                "test_case_id": case_key, "policy_id": o.policy_id,
                "baseline_decision": b_dec, "candidate_decision": c_dec,
                "expected_decision": o.expected, "result": PASS if o.status == SATISFIED else FAIL,
                "failure_type": None if o.status == SATISFIED else "POLICY_VIOLATION",
                "evidence": {"observed": c_dec, "expected": o.expected, "reason": o.reason},
            })

    # Optional case-level expectation (used sparingly; policies are the source of truth).
    allowed = expected_behavior.get("decision_in")
    if allowed and c_dec not in allowed:
        res.failure_types.append("EXPECTATION_MISMATCH")
        res.violations.append({
            "policy_id": None, "failure_type": "EXPECTATION_MISMATCH", "expected": allowed, "observed": c_dec,
            "severity": candidate_policies.severity_rules["EXPECTATION_MISMATCH"],
        })

    # ---- Baseline: informational evaluation against the baseline's own approved policies ------
    b_err = _side_error(baseline)
    if b_out is not None:
        bctx = build_context(baseline_policies, case_input, scenario, b_out, None)
        for o in evaluate_policies(baseline_policies, bctx):
            if o.status == VIOLATED:
                res.baseline_violations.append(o.as_dict())

    # ---- Version comparison -----------------------------------------------------------------
    if b_out is not None and c_dec is not None and b_dec != c_dec:
        res.changed = True
        _classify_change(res, ctx, candidate_policies, baseline_policies, case_input, scenario, b_out)

    # ---- Final state ------------------------------------------------------------------------
    if res.violations:
        res.state = FAIL
    elif res.inconclusive_reasons:
        res.state = INCONCLUSIVE
    elif b_err is not None:
        res.state = INCONCLUSIVE
        res.inconclusive_reasons.append(f"baseline unavailable ({b_err['type']}); version comparison not possible")
        res.error = {"side": "baseline", **b_err}
    elif res.changed:
        res.state = CHANGED
    else:
        res.state = PASS
    res.failure_types = sorted(set(res.failure_types))
    return res


def _evaluate_rejection(res: CaseResult, baseline: Observation | None, candidate: Observation | None,
                        cps: CompiledPolicySet) -> CaseResult:
    """Invalid input must be rejected safely: a 4xx with no decision. Accepting it, or failing
    with a 5xx, is a defect. A timeout/connection failure is a technical ERROR."""
    if candidate is None or (candidate.status == "ERROR" and candidate.error_type in
                             ("TIMEOUT", "CONNECTION_ERROR", "NO_RECORDED_OUTPUT")):
        res.state = ERROR
        res.error = {"side": "candidate", **(_side_error(candidate) or {})}
        res.failure_types = ["TECHNICAL_ERROR"]
        return res
    if candidate.status == "REJECTED":
        res.state = PASS
        res.policy_results.append({"failure_type": None, "result": PASS,
                                   "evidence": {"observed": f"HTTP {candidate.http_status}", "expected": "safe rejection (4xx)"}})
        return res
    if candidate.status == "ERROR" and (candidate.http_status or 0) >= 500:
        ftype, observed = "UNSAFE_ERROR_HANDLING", f"HTTP {candidate.http_status}"
    else:
        ftype, observed = "INVALID_INPUT_ACCEPTED", candidate.decision or f"HTTP {candidate.http_status}"
    res.state = FAIL
    res.failure_types = [ftype]
    res.violations.append({"policy_id": None, "failure_type": ftype, "expected": "safe rejection (4xx)",
                           "observed": observed, "severity": cps.severity_rules[ftype]})
    return res


def _classify_change(res: CaseResult, ctx: dict, cps: CompiledPolicySet, bps: CompiledPolicySet,
                     case_input: Any, scenario: dict, b_out: dict) -> None:
    b_dec, c_dec = res.baseline_decision, res.candidate_decision
    # 1. Change-control rules
    for rule in cps.change_controls:
        if b_dec in rule.from_ and c_dec in rule.to and eval_node(rule.when, ctx) is True:
            flag = {"rule_id": rule.rule_id, "action": rule.action, "severity": rule.severity,
                    "description": rule.description, "from": b_dec, "to": c_dec}
            res.change_flags.append(flag)
            if rule.action == "PROHIBIT":
                res.change_class = "PROHIBITED"
                res.failure_types.append("CHANGE_CONTROL_VIOLATION")
                res.violations.append({
                    "policy_id": rule.rule_id, "failure_type": "CHANGE_CONTROL_VIOLATION",
                    "expected": f"no {b_dec}→{c_dec} change", "observed": c_dec, "severity": rule.severity,
                    "reason": rule.description,
                })
    if res.change_class == "PROHIBITED":
        return
    # 2. Declared, approved changes
    for ch in cps.expected_changes:
        if b_dec in ch.from_ and c_dec in ch.to and eval_node(ch.when, ctx) is True:
            res.explained_by.append(ch.change_id)
    # 3. Approved policy revision: the baseline decision would violate the candidate's approved
    #    policy set, and the candidate decision satisfies it.
    if not any(v.get("failure_type") == "POLICY_VIOLATION" for v in res.violations):
        satisfied_now = {o["policy_id"] for o in res.policy_outcomes if o["status"] == SATISFIED}
        bctx_new = build_context(cps, case_input, scenario, b_out, None)
        for o in evaluate_policies(cps, bctx_new):
            if o.status == VIOLATED and o.policy_id in satisfied_now:
                p_new, p_old = cps.by_id(o.policy_id), bps.by_id(o.policy_id)
                revised = p_old is None or policy_fingerprint(bps, p_old) != policy_fingerprint(cps, p_new)
                res.explained_by.append(
                    f"{o.policy_id} v{o.version} (approved policy revision)" if revised
                    else f"{o.policy_id} v{o.version} (candidate corrects a baseline violation)"
                )
    if any(v.get("failure_type") == "POLICY_VIOLATION" for v in res.violations):
        # A declared/approved change never excuses a policy violation.
        res.change_class = "VIOLATION"
    else:
        res.change_class = "EXPECTED" if res.explained_by else "UNEXPECTED"
