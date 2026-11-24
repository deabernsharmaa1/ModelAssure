"""Phase 3: policies, suites, runs, worker (QA-01 … QA-08, NFR-02, NFR-04, NFR-05)."""
import json
import time

import httpx
import pytest
from sqlalchemy import create_engine, text

from scripts.demo_setup import (
    FIX, POLICY_SET_NAME, approve_policy_sets, create_standard_suite, queue_run, register_bank,
)
from workers.executor import main as worker

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def env(api, simulators):
    bank = register_bank(api.as_, simulators, system_name="Runs Engine")
    for c in bank["connections"].values():
        assert api.as_("engineer").post(f"/v1/connections/{c['id']}/verify").json()["status"] == "VERIFIED"
    policies = approve_policy_sets(api.as_, bank["project"]["id"])
    suite = create_standard_suite(api.as_, bank["project"]["id"], [policies["2026-06"]["id"], policies["2026-11"]["id"]])
    return {"bank": bank, "policies": policies, "suite": suite}


def run_to_completion(api, run_id, timeout=600):
    t0 = time.time()
    worker.drain()
    run = api.as_("analyst").get(f"/v1/runs/{run_id}").json()
    assert run["status"] == "COMPLETED", run
    return run, time.time() - t0


@pytest.fixture(scope="module")
def defective_run(api, env):
    queued = queue_run(api.as_, env["bank"], env["policies"], env["suite"], "3.2.0")
    run, seconds = run_to_completion(api, queued["run_id"])
    return {**run, "_seconds": seconds}


@pytest.fixture(scope="module")
def fixed_run(api, env):
    queued = queue_run(api.as_, env["bank"], env["policies"], env["suite"], "3.2.1")
    return run_to_completion(api, queued["run_id"])[0]


def executions(api, run_id, **params):
    return api.as_("analyst").get(f"/v1/runs/{run_id}/executions", params={"limit": 1000, **params}).json()


# ---- policy workflow ---------------------------------------------------------------------------
def test_policy_sets_versioned_and_approved(env):
    p = env["policies"]
    assert (p["2026-06"]["version"], p["2026-11"]["version"]) == (1, 2)
    assert p["2026-06"]["approval_status"] == p["2026-11"]["approval_status"] == "APPROVED"


def test_author_cannot_approve_own_policy_set(api, env):
    risk = api.as_("risk")
    content = json.loads((FIX / "policy_set_2026-11.json").read_text())
    ps = risk.post("/v1/policy-sets", json={"project_id": env["bank"]["project"]["id"], "name": "SoD probe", "content": content}).json()
    risk.post(f"/v1/policy-sets/{ps['id']}/submit", json={})
    r = risk.post(f"/v1/policy-sets/{ps['id']}/approve", json={})
    assert r.status_code == 403  # risk managers hold policy.approve, but not for their own set
    assert r.json()["error"]["code"] == "SEGREGATION_OF_DUTIES"


def test_approved_policy_set_is_immutable(api, env, database):
    ps = env["policies"]["2026-11"]
    r = api.as_("risk").put(f"/v1/policy-sets/{ps['id']}", json={"content": {"policies": []}})
    assert r.status_code == 409
    eng = create_engine(database["owner_url"].replace("modelassure:modelassure", "modelassure_app:modelassure_app"))
    with eng.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": ps["organization_id"]})
        with pytest.raises(Exception, match="immutable"):
            conn.execute(text("UPDATE policy_sets SET content='{}' WHERE id=:i"), {"i": ps["id"]})


def test_invalid_and_conflicting_policies_flagged_before_execution(api, env):
    risk = api.as_("risk")
    conflicting = {"policies": [
        {"policy_id": "A", "when": {"field": "fraud.status", "operator": "eq", "value": "HIGH"},
         "assert": {"field": "decision", "operator": "eq", "value": "HOLD"}},
        {"policy_id": "B", "when": {"field": "bureau.status", "operator": "eq", "value": "UNAVAILABLE"},
         "assert": {"field": "decision", "operator": "eq", "value": "REVIEW"}},
    ]}
    v = risk.post("/v1/policy-sets/validate", json={"content": conflicting}).json()
    assert v["valid"] is False and v["issues"][0]["code"] == "POLICY_CONFLICT"
    ps = risk.post("/v1/policy-sets", json={"project_id": env["bank"]["project"]["id"], "name": "Conflicting", "content": conflicting}).json()
    assert risk.post(f"/v1/policy-sets/{ps['id']}/submit", json={}).status_code == 422
    bad = risk.post("/v1/policy-sets/validate", json={"content": {"policies": [
        {"policy_id": "X", "assert": {"field": "decision", "operator": "exec", "value": "import os"}}]}}).json()
    assert bad["valid"] is False


def test_policy_preview(api):
    content = json.loads((FIX / "policy_set_2026-11.json").read_text())
    r = api.as_("risk").post("/v1/policy-sets/preview", json={
        "content": content, "input": {"monthly_cash_available": 13000, "monthly_debt_service": 10000},
        "output": {"decision": "APPROVE"}}).json()
    assert r["derived"]["dscr"] == 1.3
    assert {o["policy_id"]: o["status"] for o in r["outcomes"]}["POL-003"] == "VIOLATED"


def test_policy_diff_shows_parameter_change(api, env):
    p = env["policies"]
    d = api.as_("risk").get(f"/v1/policy-sets/{p['2026-11']['id']}/diff", params={"against": p["2026-06"]["id"]}).json()
    assert d["parameters"] == {"min_dscr": {"from": 1.25, "to": 1.5}}
    assert d["policies"]["POL-003"].startswith("behaviour changed")
    assert d["expected_changes"] == {"EXP-2026-11-01": "added"}


# ---- suites --------------------------------------------------------------------------------------
def test_suite_materialised_and_deterministic(api, env):
    s = env["suite"]
    assert s["case_count"] >= 1000 and s["version"] == 1
    again = create_standard_suite(api.as_, env["bank"]["project"]["id"],
                                  [env["policies"]["2026-06"]["id"], env["policies"]["2026-11"]["id"]])
    assert again["version"] == 2 and again["content_hash"] == s["content_hash"]


# ---- runs ----------------------------------------------------------------------------------------
def test_idempotent_run_creation(api, env):
    a = queue_run(api.as_, env["bank"], env["policies"], env["suite"], "3.2.0", idempotency_key="idem-123")
    b = queue_run(api.as_, env["bank"], env["policies"], env["suite"], "3.2.0", idempotency_key="idem-123")
    assert a["run_id"] == b["run_id"]
    r = api.as_("analyst").post("/v1/runs", headers={"Idempotency-Key": "idem-123"}, json={
        "project_id": env["bank"]["project"]["id"], "test_suite_id": env["suite"]["id"],
        "baseline_version_id": env["bank"]["versions"]["3.1.0"]["id"], "candidate_version_id": env["bank"]["versions"]["3.2.1"]["id"],
        "policy_set_id": env["policies"]["2026-11"]["id"]})
    assert r.status_code == 422
    api.as_("analyst").post(f"/v1/runs/{a['run_id']}/cancel")


def test_production_mode_refused(api, env):
    r = api.as_("analyst").post("/v1/runs", json={
        "project_id": env["bank"]["project"]["id"], "test_suite_id": env["suite"]["id"],
        "baseline_version_id": env["bank"]["versions"]["3.1.0"]["id"], "candidate_version_id": env["bank"]["versions"]["3.2.0"]["id"],
        "policy_set_id": env["policies"]["2026-11"]["id"], "mode": "PRODUCTION"})
    assert r.status_code == 422


def test_nfr02_thousand_cases_within_ten_minutes(defective_run):
    assert defective_run["summary"]["total"] >= 1000
    assert defective_run["_seconds"] < 600, defective_run["_seconds"]


def test_qa01_bureau_failure_approval_detected(api, defective_run):
    res = executions(api, defective_run["id"], rule="POL-001")
    assert res["total"] > 0 and all(e["result"] == "FAIL" for e in res["items"])
    # Policy requires REVIEW; the defective build skips review (mostly APPROVE, REFER when DSCR is low)
    assert all(e["baseline_decision"] == "REVIEW" and e["candidate_decision"] != "REVIEW" for e in res["items"])
    assert sum(e["candidate_decision"] == "APPROVE" for e in res["items"]) > res["total"] / 2
    assert executions(api, defective_run["id"], rule="POL-005")["total"] > 0  # live bureau timeouts


def test_qa02_fraud_override_detected(api, defective_run):
    res = executions(api, defective_run["id"], rule="POL-002")
    assert res["total"] > 0 and all(e["candidate_decision"] == "APPROVE" for e in res["items"])
    assert executions(api, defective_run["id"], rule="CHG-002")["total"] == res["total"]


def test_qa04_approved_change_recorded_without_violation(api, defective_run):
    res = executions(api, defective_run["id"], change_class="EXPECTED")
    assert res["total"] > 0
    assert any("EXP-2026-11-01" in e["summary"]["explained_by"] for e in res["items"])
    assert all(e["result"] == "CHANGED" for e in res["items"])


def test_no_technical_errors_and_baseline_clean(defective_run):
    s = defective_run["summary"]
    assert s["by_result"]["ERROR"] == 0 and s["by_result"]["INCONCLUSIVE"] == 0


def test_qa03_fixed_candidate_passes_all_policies(fixed_run):
    s = fixed_run["summary"]
    assert set(s["violations_by_rule"]) <= set(), s["violations_by_rule"]
    assert s["by_result"]["FAIL"] == 0 and s["by_result"]["ERROR"] == 0
    assert s["changes"]["expected"] > 0


def test_qa07_rerun_reproduces_identical_results(api, env, fixed_run):
    again = run_to_completion(api, queue_run(api.as_, env["bank"], env["policies"], env["suite"], "3.2.1")["run_id"])[0]
    a = {e["case_key"]: (e["result"], e["baseline_decision"], e["candidate_decision"], e["change_class"])
         for e in executions(api, fixed_run["id"])["items"]}
    b = {e["case_key"]: (e["result"], e["baseline_decision"], e["candidate_decision"], e["change_class"])
         for e in executions(api, again["id"])["items"]}
    assert a == b
    assert again["summary"]["by_result"] == fixed_run["summary"]["by_result"]


def test_qa08_policy_change_after_run_leaves_history_unchanged(api, env, defective_run):
    before = api.as_("analyst").get(f"/v1/runs/{defective_run['id']}/config").json()
    content = json.loads((FIX / "policy_set_2026-11.json").read_text())
    content["parameters"]["min_dscr"] = 2.0
    api.as_("risk").post("/v1/policy-sets", json={"project_id": env["bank"]["project"]["id"], "name": POLICY_SET_NAME,
                                                  "content": content, "parent_id": env["policies"]["2026-11"]["id"]})
    after = api.as_("analyst").get(f"/v1/runs/{defective_run['id']}/config").json()
    assert after == before and after["verified"] is True
    assert after["config"]["policy_sets"]["candidate"]["content"]["parameters"]["min_dscr"] == 1.5
    assert api.as_("analyst").get(f"/v1/runs/{defective_run['id']}").json()["summary"] == defective_run["summary"]


def test_evidence_is_stored_and_integrity_checked(api, defective_run):
    e = executions(api, defective_run["id"], rule="POL-001")["items"][0]
    full = api.as_("analyst").get(f"/v1/runs/{defective_run['id']}/executions/{e['id']}").json()
    assert full["evidence_integrity"]["verified"] is True
    ev = full["evidence"]
    assert ev["schema_versions"]["input"].startswith("canonical") and ev["snapshot_hash"]
    assert ev["candidate"]["raw_response"]["finalStatus"] == "APPROVED"
    assert ev["candidate"]["normalized"]["decision"] == "APPROVE"
    assert ev["trace_id"] and len(ev["trace_id"]) == 32


# ---- technical failures: QA-05 / QA-06 -----------------------------------------------------------
@pytest.fixture(scope="module")
def fault_run(api, env, simulators):
    base = {"product": "SME_WORKING_CAPITAL", "requested_amount": 100000, "currency": "MYR",
            "monthly_cash_available": 30000, "monthly_debt_service": 12000,
            "bureau": {"status": "AVAILABLE", "score": 760}, "fraud": {"status": "CLEAR"}, "identity": {"status": "VERIFIED"}}
    modes = {"QA05-TIMEOUT": "timeout", "QA06-MALFORMED": "malformed_json", "QA06-HTTP500": "http_500", "QA06-UNMAPPED": "unmapped_decision"}
    cases = [{"case_key": k, "category": "NORMAL", "input": {**base, "case_id": k}} for k in modes] + \
            [{"case_key": "CONTROL-OK", "category": "NORMAL", "input": {**base, "case_id": "CONTROL-OK"}}]
    suite = api.as_("analyst").post("/v1/test-suites", json={
        "project_id": env["bank"]["project"]["id"], "name": "Technical failure suite",
        "config": {"seed": 1, "generators": [{"type": "fixtures", "cases": cases}]}}).json()
    h = {"X-Control-Key": simulators["control_key"]}
    for k, mode in modes.items():
        httpx.post(f"{simulators['candidate']}/control/api-faults", json={"mode": mode, "application_ids": [k], "build": "3.2.1"}, headers=h)
    try:
        run = run_to_completion(api, queue_run(api.as_, env["bank"], env["policies"], suite, "3.2.1")["run_id"])[0]
    finally:
        httpx.delete(f"{simulators['candidate']}/control/api-faults", headers=h)
    return {e["case_key"]: e for e in executions(api, run["id"])["items"]}


@pytest.mark.parametrize("key,etype", [("QA05-TIMEOUT", "TIMEOUT"), ("QA06-MALFORMED", "MALFORMED_RESPONSE"),
                                       ("QA06-HTTP500", "HTTP_ERROR"), ("QA06-UNMAPPED", "SCHEMA_ERROR")])
def test_qa05_qa06_technical_failures_never_pass(fault_run, key, etype):
    assert fault_run[key]["result"] == "ERROR" and fault_run[key]["error_type"] == etype


def test_control_case_in_fault_suite_passes(fault_run):
    assert fault_run["CONTROL-OK"]["result"] == "PASS"


# ---- NFR-04: a crashed worker loses nothing ------------------------------------------------------
def test_worker_crash_recovery(api, env, monkeypatch):
    from modules.runner import executor

    queued = queue_run(api.as_, env["bank"], env["policies"], env["suite"], "3.2.1")
    real, calls = executor._persist, {"n": 0}

    def crashing(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 60:
            raise RuntimeError("simulated worker crash")
        return real(*a, **kw)

    monkeypatch.setattr(executor, "_persist", crashing)
    worker.run_once()
    mid = api.as_("analyst").get(f"/v1/runs/{queued['run_id']}").json()
    assert mid["status"] == "RUNNING" and executions(api, queued["run_id"])["total"] == 59
    monkeypatch.setattr(executor, "_persist", real)
    run = run_to_completion(api, queued["run_id"])[0]
    assert run["summary"]["total"] == env["suite"]["case_count"]
    assert executions(api, queued["run_id"])["total"] == env["suite"]["case_count"]
