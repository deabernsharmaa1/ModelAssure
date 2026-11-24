"""Faster onboarding: policy templates, generator profiles and the mapping helper."""
import json

import pytest

from common.hashing import content_hash
from modules.connectors.mapping import build_request
from modules.connectors.suggest import suggest_mapping
from modules.connectors.mapping import MappingError
from modules.policies.dsl import compute_derived
from modules.policies.policy_set import VIOLATED, build_context, compile_policy_set, evaluate_policies
from modules.policies.router import validate_doc
from modules.policies.templates import TemplateError, instantiate, load_templates
from modules.scenarios.generators import GenerationError, generate_suite
from scripts.demo_setup import FIX, ROOT

STANDARD_SUITE_HASH = "aefe567477334716e5d49e2198cfa3b7fe63d690b4356cc7f3c0e7e6596e7298"


def _example_params(t):
    return {p["name"]: p["example"] for p in t["parameters"]}


# ---- templates ----------------------------------------------------------------------------------
@pytest.mark.parametrize("tid", sorted(load_templates()))
def test_template_thresholds_start_empty(tid):
    t = load_templates()[tid]
    assert t["parameters"] and all(v is None for v in t["content"]["parameters"].values())
    errors = [i for i in validate_doc(instantiate(tid, {}))["issues"] if i["level"] == "error"]
    assert errors and {e["code"] for e in errors} == {"PARAMETER_NOT_SET"}


@pytest.mark.parametrize("tid", sorted(load_templates()))
def test_template_valid_once_thresholds_supplied(tid):
    t = load_templates()[tid]
    doc = instantiate(tid, _example_params(t), label="2026-12")
    v = validate_doc(doc)
    assert v["valid"], v["issues"]
    assert doc["label"] == "2026-12" and doc["source_template"]["template_id"] == tid
    assert doc["expected_changes"] == []  # approved changes are always the institution's own declarations


def test_template_rejects_unknown_and_non_numeric_parameters():
    with pytest.raises(TemplateError, match="unknown parameter"):
        instantiate("sme-lending-core", {"min_dscr": 1.4, "max_ltv": 0.8})
    with pytest.raises(TemplateError, match="must be a number"):
        instantiate("sme-lending-core", {"min_dscr": "1.4"})
    with pytest.raises(TemplateError, match="unknown template"):
        instantiate("nope", {})


def test_sme_template_matches_the_demo_policy():
    fixture = json.loads((FIX / "policy_set_2026-11.json").read_text())
    doc = instantiate("sme-lending-core", dict(fixture["parameters"]))
    assert doc["policies"] == fixture["policies"] and doc["change_controls"] == fixture["change_controls"]


def test_consumer_template_affordability_rule():
    cps = compile_policy_set(instantiate("consumer-credit-affordability", {"max_dsr": 0.6}))
    base = {"case_id": "C", "requested_amount": 3000, "requested_instalment": 250, "monthly_net_income": 5000,
            "income_verified": True, "bureau": {"status": "AVAILABLE"}, "fraud": {"status": "CLEAR"},
            "identity": {"status": "VERIFIED"}}

    def failed(inp, decision):
        ctx = build_context(cps, inp, {"faults": {}}, {"decision": decision}, None)
        return {o.policy_id for o in evaluate_policies(cps, ctx) if o.status == VIOLATED}

    assert failed({**base, "monthly_commitments": 2760}, "APPROVE") == {"CC-002"}  # dsr 0.602 > 0.6
    assert failed({**base, "monthly_commitments": 2750}, "APPROVE") == set()       # exactly 0.6 is allowed
    assert failed({**base, "monthly_commitments": 2760}, "DECLINE") == set()
    no_income = {k: v for k, v in base.items() if k != "monthly_net_income"} | {"monthly_commitments": 100}
    assert "CC-001" in failed(no_income, "APPROVE")
    assert failed({**base, "monthly_commitments": 100, "income_verified": False}, "APPROVE") == {"CC-003"}


# ---- generator profiles -------------------------------------------------------------------------
def _standard_cfg(**extra):
    cfg = json.loads((ROOT / "test_suites/lending/standard_release_suite.json").read_text())
    return {"seed": cfg["seed"], "generators": cfg["generators"], **extra}


def test_standard_sme_suite_is_pinned():
    """Same config + seed + GENERATOR_VERSION must give byte-identical suites on any machine."""
    b = compile_policy_set(json.loads((FIX / "policy_set_2026-06.json").read_text()))
    c = compile_policy_set(json.loads((FIX / "policy_set_2026-11.json").read_text()))
    assert content_hash(generate_suite(_standard_cfg(), [b, c])) == STANDARD_SUITE_HASH, "bump GENERATOR_VERSION"
    # Naming the default profile explicitly changes only the suite tag inside case ids.
    def strip(cases):
        def no_id(x):
            if isinstance(x, dict):
                return {k: v for k, v in x.items() if k != "case_id"}
            return [no_id(i) for i in x] if isinstance(x, list) else x
        return [{**c, "input": no_id(c["input"])} for c in cases]
    assert strip(generate_suite(_standard_cfg(profile="sme_lending"), [b, c])) == strip(generate_suite(_standard_cfg(), [b, c]))


def test_consumer_profile_is_deterministic_and_hits_threshold_exactly():
    cps = compile_policy_set(instantiate("consumer-credit-affordability", {"max_dsr": 0.6}))
    a = generate_suite(_standard_cfg(profile="consumer_credit"), [cps])
    assert content_hash(a) == content_hash(generate_suite(_standard_cfg(profile="consumer_credit"), [cps]))
    cats = {c["category"] for c in a}
    assert {"NORMAL", "BOUNDARY", "MISSING_DATA", "DEPENDENCY_FAILURE", "CROSS_SYSTEM_CONFLICT",
            "DUPLICATE", "INVALID_INPUT", "VERSION_REGRESSION"} <= cats
    dsrs = sorted({compute_derived(cps.derived, {"input": c["input"], "params": cps.parameters, "scenario": {}})["dsr"]
                   for c in a if c["category"] == "BOUNDARY"})
    assert dsrs == [0.59, 0.6, 0.61]
    assert all("monthly_net_income" in c["input"] for c in a if c["category"] == "NORMAL")


def test_unknown_profile_rejected():
    with pytest.raises(GenerationError, match="unknown profile"):
        generate_suite({"seed": 1, "profile": "insurance", "generators": [{"type": "normal"}]}, [])


# ---- mapping helper -----------------------------------------------------------------------------
TANJUNG = json.loads((FIX / "tanjung_mapping.json").read_text())
CANONICAL_INPUT = {"case_id": "C1", "product": "SME_WORKING_CAPITAL", "requested_amount": 100000, "currency": "MYR",
                   "monthly_cash_available": 25000, "monthly_debt_service": 12000,
                   "bureau": {"status": "AVAILABLE", "score": 700}, "fraud": {"status": "CLEAR", "alert_resolved": False},
                   "identity": {"status": "VERIFIED"}}


def _tanjung_response(status):
    return {"applicationId": "C1", "finalStatus": status, "probabilityOfDefault": 0.02, "modelVersion": "3.1.0",
            "policyVersion": "2026-06", "reasonCodes": [], "traceId": "t-1"}


def test_suggestion_recovers_the_banks_mapping():
    r = suggest_mapping(build_request(TANJUNG, CANONICAL_INPUT),
                        [_tanjung_response(s) for s in ("APPROVED", "DECLINED", "MANUAL_REVIEW", "ON_HOLD", "REFERRED")])
    assert r["valid"] and r["mapping"] == TANJUNG
    assert all(c["ok"] for c in r["response_checks"])
    assert r["type_mismatches"] == [] and r["unmatched"] == {"request": [], "response": []}


def test_suggestion_only_maps_decision_values_it_has_seen():
    r = suggest_mapping(None, [_tanjung_response("APPROVED")])
    assert r["mapping"]["response"]["fields"]["decision"]["values"] == {"APPROVED": "APPROVE"}
    assert any("DECLINE" in n and "ERROR, never a pass" in n for n in r["notes"])


def test_suggestion_for_a_canonical_api_is_empty():
    r = suggest_mapping(CANONICAL_INPUT, [{"case_id": "C1", "decision": "APPROVE", "pd": 0.1}])
    assert r["mapping"] == {} and r["valid"]


def test_suggestion_flags_values_with_the_wrong_shape():
    r = suggest_mapping({"caseId": "x", "fraudFlag": True}, [{"outcome": "Approved", "riskScore": 640}])
    mism = {m["customer_field"]: m["canonical_field"] for m in r["type_mismatches"]}
    assert mism == {"fraudFlag": "fraud.status", "riskScore": "pd"}
    assert "fraud.status" not in r["mapping"]["request"]["fields"].values()
    assert "pd" not in r["mapping"]["response"]["fields"]


def test_suggestion_reports_unknown_decision_words():
    r = suggest_mapping(None, [{"result": "MAYBE"}])
    assert any("MAYBE" in n for n in r["notes"])
    assert r["response_checks"][0]["ok"] is False


def test_suggestion_requires_a_response():
    with pytest.raises(MappingError):
        suggest_mapping({"a": 1}, [])
