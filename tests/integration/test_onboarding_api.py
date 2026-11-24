"""Faster onboarding through the public API: templates, mapping suggestions, setup checklist."""
import json

import pytest

from scripts.demo_setup import FIX

pytestmark = pytest.mark.integration


def _ok(r, code=(200, 201)):
    assert r.status_code in code, r.text
    return r.json()


def test_templates_listed_without_thresholds(api):
    ts = _ok(api.as_("analyst").get("/v1/policy-templates"))
    ids = {t["template_id"] for t in ts}
    assert {"sme-lending-core", "consumer-credit-affordability"} <= ids
    for t in ts:
        assert t["parameters"] and t["policies"] and t["template_hash"]
    cc = _ok(api.as_("analyst").get("/v1/policy-templates/consumer-credit-affordability"))
    assert cc["content"]["parameters"] == {"max_dsr": None} and "not legal advice" in cc["legal_note"]
    assert api.as_("analyst").get("/v1/policy-templates/nope").status_code == 404


def test_instantiate_requires_policy_edit_and_valid_parameters(api):
    assert api.as_("analyst").post("/v1/policy-templates/sme-lending-core/instantiate", json={}).status_code == 403
    r = api.as_("risk").post("/v1/policy-templates/sme-lending-core/instantiate", json={"parameters": {"min_dscr": "high"}})
    assert r.status_code == 422
    blank = _ok(api.as_("risk").post("/v1/policy-templates/sme-lending-core/instantiate", json={}))
    assert not blank["validation"]["valid"]
    assert {i["code"] for i in blank["validation"]["issues"] if i["level"] == "error"} == {"PARAMETER_NOT_SET"}


def _fresh_project(api, simulators, name, category="SME_LENDING_DECISION"):
    eng, risk = api.as_("engineer"), api.as_("risk")
    system = _ok(eng.post("/v1/systems", json={"name": name, "category": category}))
    v1 = _ok(eng.post(f"/v1/systems/{system['id']}/versions", json={"version": "3.1.0", "expected_model_version": "3.1.0"}))
    v2 = _ok(eng.post(f"/v1/systems/{system['id']}/versions", json={"version": "3.2.1", "expected_model_version": "3.2.1"}))
    users = {u["email"]: u for u in _ok(risk.get("/v1/users"))}
    project = _ok(risk.post("/v1/projects", json={
        "name": name, "business_use_case": "Onboarding test", "system_id": system["id"],
        "owner_id": users["risk@tanjung.example"]["id"], "baseline_version_id": v1["id"], "candidate_version_id": v2["id"],
        "risk_classification": "HIGH", "test_environment": "Simulator"}))
    return project, {"3.1.0": v1, "3.2.1": v2}


def test_template_to_approved_policy_and_setup_checklist(api, simulators):
    eng, risk, reviewer = api.as_("engineer"), api.as_("risk"), api.as_("reviewer")
    project, versions = _fresh_project(api, simulators, "Onboarding — SME")
    setup = _ok(risk.get(f"/v1/projects/{project['id']}/setup"))
    assert setup["completed"] == 1 and setup["next_step"] == "connections"

    # 1. Mapping helper drafts the bank's mapping from sample payloads (in the bank's own format)
    sample_req = {"applicationId": "X-1", "productCode": "SME_WORKING_CAPITAL", "requestedAmount": 100000, "currency": "MYR",
                  "monthlyCashAvailable": 25000, "monthlyDebtService": 12000,
                  "bureau": {"status": "AVAILABLE", "score": 700}, "fraud": {"status": "CLEAR", "alertResolved": False},
                  "identity": {"status": "VERIFIED"}}
    sample_resps = [{"applicationId": "X-1", "finalStatus": s, "probabilityOfDefault": 0.02, "modelVersion": "3.1.0",
                     "policyVersion": "2026-06", "reasonCodes": [], "traceId": "t"}
                    for s in ("APPROVED", "DECLINED", "MANUAL_REVIEW", "ON_HOLD", "REFERRED")]
    assert risk.post("/v1/connections/suggest-mapping", json={"sample_responses": sample_resps}).status_code == 403
    sug = _ok(eng.post("/v1/connections/suggest-mapping", json={"sample_request": sample_req, "sample_responses": sample_resps}))
    assert sug["valid"] and sug["mapping"] == json.loads((FIX / "tanjung_mapping.json").read_text())
    assert eng.post("/v1/connections/suggest-mapping", json={"sample_responses": []}).status_code == 422

    # 2. Connections created with the suggested mapping verify against the real (fictional) bank
    for v, side in (("3.1.0", "baseline"), ("3.2.1", "candidate")):
        c = _ok(eng.post("/v1/connections", json={
            "system_version_id": versions[v]["id"], "kind": "REST", "endpoint": f"{simulators[side]}/{v}/decisions",
            "auth_header": "X-API-Key", "credential": simulators["api_key"], "mapping": sug["mapping"],
            "dependency_control_url": simulators["dependency"], "dependency_credential": simulators["dep_control_key"]}))
        assert _ok(eng.post(f"/v1/connections/{c['id']}/verify"))["status"] == "VERIFIED"
    assert _ok(risk.get(f"/v1/projects/{project['id']}/setup"))["next_step"] == "policies"

    # 3. A blank template cannot be submitted; with the bank's own threshold it can be approved
    blank = _ok(risk.post("/v1/policy-templates/sme-lending-core/instantiate", json={}))["content"]
    ps = _ok(risk.post("/v1/policy-sets", json={"project_id": project["id"], "name": "From template", "content": blank}))
    r = risk.post(f"/v1/policy-sets/{ps['id']}/submit", json={"note": "x"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "POLICY_INVALID"
    filled = _ok(risk.post("/v1/policy-templates/sme-lending-core/instantiate",
                           json={"parameters": {"min_dscr": 1.5}, "label": "2026-12"}))["content"]
    assert _ok(risk.put(f"/v1/policy-sets/{ps['id']}", json={"content": filled}))["validation"]["valid"]
    _ok(risk.post(f"/v1/policy-sets/{ps['id']}/submit", json={"note": "From the SME template"}))
    _ok(reviewer.post(f"/v1/policy-sets/{ps['id']}/approve", json={"note": "ok"}))
    after = _ok(risk.get(f"/v1/projects/{project['id']}/setup"))
    assert after["completed"] == 3 and after["next_step"] == "suite"
    stored = _ok(risk.get(f"/v1/policy-sets/{ps['id']}"))
    assert stored["content"]["source_template"]["template_id"] == "sme-lending-core"

    # 4. Suite generation records the generator version used
    suite = _ok(api.as_("analyst").post("/v1/test-suites", json={
        "project_id": project["id"], "name": "Template suite",
        "config": {"seed": 7, "generators": [{"type": "normal"}, {"type": "boundary"}]}, "policy_set_ids": [ps["id"]]}))
    assert suite["generator_config"]["generator_version"] == 2
    assert _ok(risk.get(f"/v1/projects/{project['id']}/setup"))["next_step"] == "run"


def test_consumer_credit_project_and_suite(api, simulators):
    project, _ = _fresh_project(api, simulators, "Onboarding — BNPL", category="CONSUMER_CREDIT_DECISION")
    risk = api.as_("risk")
    doc = _ok(risk.post("/v1/policy-templates/consumer-credit-affordability/instantiate",
                        json={"parameters": {"max_dsr": 0.6}}))
    assert doc["validation"]["valid"]
    ps = _ok(risk.post("/v1/policy-sets", json={"project_id": project["id"], "name": "BNPL", "content": doc["content"]}))
    prev = _ok(api.as_("analyst").post("/v1/test-suites/preview", json={
        "project_id": project["id"], "name": "BNPL suite",
        "config": {"seed": 3, "profile": "consumer_credit", "generators": [{"type": "boundary"}, {"type": "missing_data"}]},
        "policy_set_ids": [ps["id"]]}))
    assert prev["categories"]["BOUNDARY"] == 9 and "monthly_net_income" in prev["sample"][0]["input"]
    bad = api.as_("analyst").post("/v1/test-suites/preview", json={
        "project_id": project["id"], "name": "x", "config": {"profile": "insurance", "generators": [{"type": "normal"}]}})
    assert bad.status_code == 422
