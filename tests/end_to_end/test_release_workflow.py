"""Phase 4 end-to-end: the three demo scenarios through findings, retest, review and reports
(QA-01, QA-02, QA-04, QA-10, QA-11, QA-12 and the spec §18 Definition of Done)."""
import csv
import io
import json

import pytest

from scripts.demo_setup import FIX, approve_policy_sets, create_standard_suite, queue_run, register_bank
from workers.executor import main as worker

pytestmark = pytest.mark.integration


def complete(api, run_id):
    worker.drain()
    run = api.as_("analyst").get(f"/v1/runs/{run_id}").json()
    assert run["status"] == "COMPLETED", run
    return run


@pytest.fixture(scope="module")
def flow(api, simulators):
    bank = register_bank(api.as_, simulators, system_name="E2E Engine")
    for c in bank["connections"].values():
        api.as_("engineer").post(f"/v1/connections/{c['id']}/verify")
    policies = approve_policy_sets(api.as_, bank["project"]["id"])
    suite = create_standard_suite(api.as_, bank["project"]["id"], [policies["2026-06"]["id"], policies["2026-11"]["id"]])
    run = complete(api, queue_run(api.as_, bank, policies, suite, "3.2.0")["run_id"])
    return {"bank": bank, "policies": policies, "suite": suite, "run": run}


def findings(api, run_id, **params):
    return api.as_("analyst").get("/v1/findings", params={"run_id": run_id, **params}).json()["items"]


def by_rule(items, rule):
    return next(f for f in items if f["rule_ids"] and f["rule_ids"][0] == rule)


# ---- findings and assessment of the defective release ----------------------------------------------
def test_defective_release_fails_assessment(flow):
    a = flow["run"]["assessment"]
    assert a["outcome"] == "FAIL"
    assert any("Mandatory bureau data unavailable" in r for r in a["fail_reasons"])
    assert any("high-risk fraud alert" in r for r in a["fail_reasons"])
    assert "not a statement that the system is free of risk" in a["disclaimer"]


def test_findings_grouped_with_evidence_and_suspected_cause(api, flow):
    items = findings(api, flow["run"]["id"])
    pol1 = by_rule(items, "POL-001")
    assert pol1["severity"] == "CRITICAL" and pol1["status"] == "OPEN" and pol1["affected_count"] > 0
    assert "BUREAU_DEFAULT_SCORE_APPLIED" in pol1["suspected_cause"]
    pol2 = by_rule(items, "POL-002")
    # One finding per root cause: the primary rule leads, every co-violated rule is listed.
    assert pol2["rule_ids"][0] == "POL-002" and "CHG-002" in pol2["rule_ids"]
    assert sum("POL-002" in f["rule_ids"] for f in items) == 1
    assert "FRAUD_ALERT_OVERRIDDEN_PRIME" in pol2["suspected_cause"]
    assert any(f["type"] == "UNEXPECTED_CHANGE" for f in items)
    detail = api.as_("analyst").get(f"/v1/findings/{pol1['id']}").json()
    assert detail["executions"] and all(e["result"] == "FAIL" for e in detail["executions"])


def test_expected_change_creates_no_finding(api, flow):
    # QA-04: approved DSCR change is visible in metrics but never becomes a finding.
    assert flow["run"]["summary"]["changes"]["expected"] > 0
    assert not any("EXP-2026-11-01" in f["rule_ids"] for f in findings(api, flow["run"]["id"]))


# ---- QA-12: release sign-off controls ---------------------------------------------------------------
def test_qa12_unauthorised_signoff_denied_and_logged(api, flow):
    r = api.as_("engineer").post(f"/v1/runs/{flow['run']['id']}/reviews", json={"decision": "APPROVE_RELEASE", "rationale": "Looks fine to me"})
    assert r.status_code == 403
    events = api.as_("auditor").get("/v1/audit-events", params={"action": "access.denied"}).json()["items"]
    assert any(e["details"]["permission"] == "release.review" and e["actor_label"] == "engineer@tanjung.example" for e in events)


def test_builder_cannot_approve_own_release(api, flow):
    lead = api.as_("lead@tanjung.example")
    v = lead.post(f"/v1/systems/{flow['bank']['system']['id']}/versions", json={"version": "3.2.0-lead", "expected_model_version": "3.2.0"}).json()
    c = lead.post("/v1/connections", json={"system_version_id": v["id"], "endpoint": flow["bank"]["connections"]["3.2.0"]["endpoint"],
                                           "auth_header": "X-API-Key", "credential": "sim-test-key",
                                           "mapping": json.loads((FIX / "tanjung_mapping.json").read_text())}).json()
    assert lead.post(f"/v1/connections/{c['id']}/verify").json()["status"] == "VERIFIED"
    run = complete(api, queue_run(api.as_, {**flow["bank"], "versions": {**flow["bank"]["versions"], "3.2.0-lead": v}},
                                  flow["policies"], flow["suite"], "3.2.0-lead")["run_id"])
    r = lead.post(f"/v1/runs/{run['id']}/reviews", json={"decision": "REJECT_RELEASE", "rationale": "my own build"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "SEGREGATION_OF_DUTIES"


def test_failed_release_cannot_be_approved(api, flow):
    r = api.as_("reviewer").post(f"/v1/runs/{flow['run']['id']}/reviews", json={"decision": "APPROVE_RELEASE", "rationale": "Approving anyway"})
    assert r.status_code == 409 and r.json()["error"]["details"]["fail_reasons"]
    ok = api.as_("reviewer").post(f"/v1/runs/{flow['run']['id']}/reviews",
                                  json={"decision": "REQUEST_REMEDIATION", "rationale": "Fix POL-001 and POL-002 findings, then retest."})
    assert ok.status_code == 201


# ---- finding lifecycle, risk acceptance ----------------------------------------------------------------
def test_finding_transitions_enforced(api, flow):
    f = by_rule(findings(api, flow["run"]["id"]), "POL-001")
    assert api.as_("analyst").patch(f"/v1/findings/{f['id']}", json={"status": "RESOLVED"}).status_code == 422
    assert api.as_("analyst").patch(f"/v1/findings/{f['id']}", json={"status": "READY_FOR_RETEST"}).status_code == 409


def test_risk_acceptance_requires_authority_owner_and_rationale(api, flow):
    target = next(f for f in findings(api, flow["run"]["id"]) if f["type"] == "UNEXPECTED_CHANGE")
    risk_owner = flow["bank"]["users"]["risk@tanjung.example"]["id"]
    body = {"status": "ACCEPTED_RISK", "risk_acceptance": {"owner_id": risk_owner, "rationale": "Model 3.2 recalibration reviewed by credit risk; acceptable."}}
    assert api.as_("analyst").patch(f"/v1/findings/{target['id']}", json=body).status_code == 403
    assert api.as_("risk").patch(f"/v1/findings/{target['id']}", json={"status": "ACCEPTED_RISK"}).status_code == 422
    r = api.as_("risk").patch(f"/v1/findings/{target['id']}", json=body)
    assert r.status_code == 200 and r.json()["risk_acceptance"]["owner_email"] == "risk@tanjung.example"


# ---- QA-10: fix and retest ------------------------------------------------------------------------
@pytest.fixture(scope="module")
def retested(api, flow):
    items = findings(api, flow["run"]["id"])
    targets = [f for f in items if f["type"] in ("POLICY_VIOLATION", "CHANGE_CONTROL_VIOLATION")]
    eng_id = flow["bank"]["users"]["engineer@tanjung.example"]["id"]
    for f in targets:
        for step in ({"owner_id": eng_id, "status": "INVESTIGATING"}, {"status": "FIX_PROPOSED", "suspected_cause": f["suspected_cause"]},
                     {"status": "READY_FOR_RETEST", "comment": "Fixed in build 3.2.1"}):
            r = api.as_("engineer").patch(f"/v1/findings/{f['id']}", json=step)
            assert r.status_code == 200, r.text
    before = api.as_("analyst").get(f"/v1/runs/{flow['run']['id']}/executions", params={"result": "FAIL", "limit": 1000}).json()
    rt = api.as_("analyst").post(f"/v1/runs/{flow['run']['id']}/retest",
                                 json={"candidate_version_id": flow["bank"]["versions"]["3.2.1"]["id"], "scope": "AFFECTED"})
    assert rt.status_code == 201, rt.text
    retest_run = complete(api, rt.json()["run_id"])
    return {"targets": targets, "before": before, "retest_run": retest_run}


def test_qa10_retest_passes_and_resolves_findings(api, flow, retested):
    rr = retested["retest_run"]
    assert rr["scope"] == "RETEST" and rr["summary"]["by_result"]["FAIL"] == 0
    for f in retested["targets"]:
        now = api.as_("analyst").get(f"/v1/findings/{f['id']}").json()
        assert now["status"] == "RESOLVED"
        assert now["retest_history"][-1]["passed"] is True and now["retest_history"][-1]["candidate_version"] == "3.2.1"


def test_qa10_historical_evidence_retained(api, flow, retested):
    after = api.as_("analyst").get(f"/v1/runs/{flow['run']['id']}/executions", params={"result": "FAIL", "limit": 1000}).json()
    assert after == retested["before"]


def test_fix_in_other_build_does_not_make_original_releasable(api, flow, retested):
    a = api.as_("analyst").get(f"/v1/runs/{flow['run']['id']}/assessment").json()
    assert a["outcome"] == "FAIL" and any("was fixed in 3.2.1" in r for r in a["fail_reasons"])


def test_partial_retest_is_not_a_release_basis(api, retested):
    a = api.as_("analyst").get(f"/v1/runs/{retested['retest_run']['id']}/assessment").json()
    assert a["outcome"] == "INCONCLUSIVE"


# ---- full run of the fixed build → human approval ----------------------------------------------------
@pytest.fixture(scope="module")
def fixed_full(api, flow, retested):
    return complete(api, queue_run(api.as_, flow["bank"], flow["policies"], flow["suite"], "3.2.1")["run_id"])


def test_fixed_build_passes_with_flags_and_can_be_approved(api, fixed_full):
    a = fixed_full["assessment"]
    assert a["outcome"] == "PASS", a
    assert any("unexpected" in f for f in a["flags"])
    r = api.as_("reviewer").post(f"/v1/runs/{fixed_full['id']}/reviews", json={
        "decision": "APPROVE_RELEASE", "rationale": "All critical policy findings resolved; unexpected DECLINE→REFER changes reviewed with Credit Risk."})
    assert r.status_code == 201 and "does not deploy" in r.json()["note"]
    assert api.as_("analyst").get(f"/v1/runs/{fixed_full['id']}/reviews").json()[0]["decision"] == "APPROVE_RELEASE"


# ---- QA-11: reports ---------------------------------------------------------------------------------
def test_qa11_reports_versioned_and_reproducible(api, fixed_full):
    an = api.as_("analyst")
    v1 = an.post(f"/v1/runs/{fixed_full['id']}/report").json()
    v2 = an.post(f"/v1/runs/{fixed_full['id']}/report").json()
    assert (v1["version"], v2["version"]) == (1, 2)
    assert v1["results_digest"] == v2["results_digest"]
    j1 = an.get(f"/v1/runs/{fixed_full['id']}/report", params={"format": "json", "version": 1})
    assert j1.status_code == 200 and j1.headers["X-Report-Version"] == "1"
    rep = j1.json()
    assert rep["run"]["snapshot_hash"] == fixed_full["snapshot_hash"]
    for section in ("executive_summary", "methodology", "coverage", "decision_changes", "policy_violations",
                    "financial_implications", "limitations", "remediation", "review"):
        assert section in rep
    assert rep["financial_implications"]["assessed"] is False
    pdf = an.get(f"/v1/runs/{fixed_full['id']}/report", params={"format": "pdf"})
    assert pdf.status_code == 200 and pdf.content[:5] == b"%PDF-" and len(pdf.content) > 20_000


def test_report_for_failed_run_lists_reproducible_violations(api, flow):
    rep = api.as_("risk").get(f"/v1/runs/{flow['run']['id']}/report").json()
    v = next(x for x in rep["policy_violations"] if "POL-001" in x["rules"])
    assert v["reproducible_cases"] and v["severity"] == "CRITICAL"
    assert rep["executive_summary"]["assessment"] == "FAIL"


def test_csv_export_contains_only_permitted_outcomes(api, flow):
    r = api.as_("analyst").get(f"/v1/runs/{flow['run']['id']}/executions.csv")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == flow["run"]["summary"]["total"]
    assert "requested_amount" not in rows[0] and "input" not in rows[0]


def test_audit_chain_still_valid_after_full_workflow(api):
    assert api.as_("auditor").get("/v1/audit-events/verify").json()["valid"] is True


def test_retention_purge_keeps_outcomes_and_hashes(api, flow, database):
    """Spec §12.2: raw records have limited retention; outcomes and hashes remain provable."""
    from datetime import datetime, timedelta, timezone

    from scripts.purge_evidence import purge

    org = flow["run"]["organization_id"]
    ex = api.as_("analyst").get(f"/v1/runs/{flow['run']['id']}/executions", params={"rule": "POL-001", "limit": 1}).json()["items"][0]
    assert purge(org, 1, dry_run=True)["evidence_objects"] == 0  # nothing older than a day yet
    future = (datetime.now(timezone.utc) + timedelta(days=400))
    import scripts.purge_evidence as pe
    real_now = pe.datetime

    class Shifted(datetime):
        @classmethod
        def now(cls, tz=None):
            return future

    pe.datetime = Shifted
    try:
        result = purge(org, 365, url=database["owner_url"])
    finally:
        pe.datetime = real_now
    assert result["evidence_objects"] > 0
    full = api.as_("analyst").get(f"/v1/runs/{flow['run']['id']}/executions/{ex['id']}").json()
    assert full["evidence"] is None and full["evidence_integrity"]["purged"] is True
    assert full["result"] == "FAIL" and full["evidence_hash"] == ex["evidence_hash"]
    events = api.as_("auditor").get("/v1/audit-events", params={"action": "evidence.purge"}).json()
    assert events["total"] >= 1
    assert api.as_("auditor").get("/v1/audit-events/verify").json()["valid"] is True
