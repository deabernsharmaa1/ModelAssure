"""Result-state rules of the evaluation engine (spec §8.1, Module E)."""
import json
from pathlib import Path

import pytest

from modules.evaluation.engine import (
    CHANGED, ERROR, FAIL, INCONCLUSIVE, PASS, SKIPPED, Observation, evaluate_case,
)
from modules.policies.policy_set import compile_policy_set
from modules.scenarios.generators import default_scenario

FIX = Path(__file__).resolve().parents[2] / "decision_simulator" / "fixtures"
BASE = compile_policy_set(json.loads((FIX / "policy_set_2026-06.json").read_text()))
CAND = compile_policy_set(json.loads((FIX / "policy_set_2026-11.json").read_text()))

GOOD = {"case_id": "C1", "requested_amount": 100000, "currency": "MYR", "product": "SME_WORKING_CAPITAL",
        "monthly_cash_available": 25000, "monthly_debt_service": 10000,
        "bureau": {"status": "AVAILABLE", "score": 760}, "fraud": {"status": "CLEAR"}, "identity": {"status": "VERIFIED"}}


def ok(decision, **kw):
    return Observation(status="OK", http_status=200, normalized={"decision": decision, "model_version": "x", **kw})


def run(inp=GOOD, b=None, c=None, category="NORMAL", scenario=None, expected=None, skip=None):
    return evaluate_case(case_key="K", category=category, case_input=inp, scenario=scenario or default_scenario(),
                         expected_behavior=expected, baseline=b, candidate=c,
                         candidate_policies=CAND, baseline_policies=BASE, skip_reason=skip)


def test_pass():
    r = run(b=ok("APPROVE"), c=ok("APPROVE"))
    assert r.state == PASS and not r.changed


def test_qa01_bureau_unavailable_approve_is_policy_violation():
    inp = {**GOOD, "bureau": {"status": "UNAVAILABLE"}}
    r = run(inp, b=ok("REVIEW"), c=ok("APPROVE"))
    assert r.state == FAIL
    assert r.violations[0]["policy_id"] == "POL-001" and r.violations[0]["expected"] == "REVIEW"
    # spec §7.4 evaluation row
    row = next(x for x in r.policy_results if x["policy_id"] == "POL-001")
    assert row == {"test_case_id": "K", "policy_id": "POL-001", "baseline_decision": "REVIEW",
                   "candidate_decision": "APPROVE", "expected_decision": "REVIEW", "result": "FAIL",
                   "failure_type": "POLICY_VIOLATION",
                   "evidence": {"observed": "APPROVE", "expected": "REVIEW",
                                "reason": "Mandatory bureau data unavailable requires manual review"}}


def test_qa02_fraud_override_fails_policy_and_change_control():
    inp = {**GOOD, "fraud": {"status": "HIGH"}}
    r = run(inp, b=ok("HOLD"), c=ok("APPROVE"))
    assert r.state == FAIL
    assert {v["policy_id"] for v in r.violations} == {"POL-002", "CHG-002"}
    assert r.change_class == "PROHIBITED"


def test_resolved_fraud_alert_does_not_require_hold():
    inp = {**GOOD, "fraud": {"status": "HIGH", "alert_resolved": True}}
    assert run(inp, b=ok("APPROVE"), c=ok("APPROVE")).state == PASS


def test_qa04_approved_policy_change_is_expected_change_not_violation():
    inp = {**GOOD, "monthly_cash_available": 13000, "monthly_debt_service": 10000}  # dscr 1.3
    r = run(inp, b=ok("APPROVE"), c=ok("REFER"))
    assert r.state == CHANGED and r.change_class == "EXPECTED" and not r.violations
    assert "EXP-2026-11-01" in r.explained_by
    assert any("POL-003" in e and "approved policy revision" in e for e in r.explained_by)


def test_unexplained_change_is_unexpected():
    r = run(b=ok("REFER"), c=ok("APPROVE"))
    assert r.state == CHANGED and r.change_class == "UNEXPECTED"


def test_flag_change_control_recorded():
    r = run(b=ok("DECLINE"), c=ok("APPROVE"))
    assert r.state == CHANGED and r.change_flags[0]["rule_id"] == "CHG-001"


@pytest.mark.parametrize("etype", ["TIMEOUT", "MALFORMED_RESPONSE", "SCHEMA_ERROR", "CONNECTION_ERROR", "VERSION_MISMATCH"])
def test_qa05_qa06_candidate_technical_errors_are_never_pass(etype):
    r = run(b=ok("APPROVE"), c=Observation(status="ERROR", error_type=etype))
    assert r.state == ERROR and r.error["side"] == "candidate" and r.error["type"] == etype


def test_baseline_error_makes_case_inconclusive_not_pass():
    r = run(b=Observation(status="ERROR", error_type="TIMEOUT"), c=ok("APPROVE"))
    assert r.state == INCONCLUSIVE and "baseline unavailable" in r.inconclusive_reasons[0]


def test_baseline_error_does_not_hide_candidate_violation():
    inp = {**GOOD, "bureau": {"status": "UNAVAILABLE"}}
    r = run(inp, b=Observation(status="ERROR", error_type="TIMEOUT"), c=ok("APPROVE"))
    assert r.state == FAIL


def test_missing_condition_data_is_inconclusive():
    doc = {"policies": [{"policy_id": "P", "when": {"field": "segment", "operator": "eq", "value": "X"},
                         "assert": {"field": "decision", "operator": "ne", "value": "APPROVE"}}]}
    cps = compile_policy_set(doc)
    r = evaluate_case(case_key="K", category="NORMAL", case_input={"a": 1}, scenario=default_scenario(),
                      expected_behavior=None, baseline=ok("APPROVE"), candidate=ok("APPROVE"),
                      candidate_policies=cps, baseline_policies=cps)
    assert r.state == INCONCLUSIVE


def test_invalid_input_rejected_is_pass():
    rej = Observation(status="REJECTED", http_status=422)
    assert run({"bad": 1}, b=rej, c=rej, category="INVALID_INPUT").state == PASS


def test_invalid_input_accepted_is_fail():
    r = run({"bad": 1}, b=Observation(status="REJECTED", http_status=422), c=ok("APPROVE"), category="INVALID_INPUT")
    assert r.state == FAIL and r.failure_types == ["INVALID_INPUT_ACCEPTED"]


def test_invalid_input_500_is_unsafe_error_handling():
    r = run({"bad": 1}, c=Observation(status="ERROR", error_type="HTTP_ERROR", http_status=500), category="INVALID_INPUT")
    assert r.state == FAIL and r.failure_types == ["UNSAFE_ERROR_HANDLING"]


def test_invalid_input_timeout_is_error():
    r = run({"bad": 1}, c=Observation(status="ERROR", error_type="TIMEOUT"), category="INVALID_INPUT")
    assert r.state == ERROR


def test_inconsistent_duplicates_fail():
    c = ok("APPROVE")
    c.repeats = [ok("APPROVE"), ok("REFER")]
    r = run(b=ok("APPROVE"), c=c, category="DUPLICATE")
    assert r.state == FAIL and "INCONSISTENT_DUPLICATE" in r.failure_types


def test_dependency_timeout_requires_fallback():
    inp = {k: v for k, v in GOOD.items() if k != "bureau"}
    r = run(inp, b=ok("REVIEW"), c=ok("APPROVE"), scenario=default_scenario(faults={"bureau": "TIMEOUT"}))
    assert r.state == FAIL and r.violations[0]["policy_id"] == "POL-005"


def test_skipped():
    assert run(b=ok("APPROVE"), c=ok("APPROVE"), skip="excluded by analyst").state == SKIPPED


def test_precedence_fraud_hold_beats_bureau_review():
    inp = {**GOOD, "bureau": {"status": "UNAVAILABLE"}, "fraud": {"status": "HIGH"}}
    r = run(inp, b=ok("HOLD"), c=ok("HOLD"))
    assert r.state == PASS
    statuses = {o["policy_id"]: o["status"] for o in r.policy_outcomes}
    assert statuses["POL-001"] == "SUPERSEDED" and statuses["POL-002"] == "SATISFIED"
