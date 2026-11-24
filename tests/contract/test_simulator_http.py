"""Contract tests for the fictional bank's HTTP APIs and the dependency simulator."""
import httpx
import pytest

pytestmark = pytest.mark.integration

REQ = {"applicationId": "CT-1", "productCode": "SME_WORKING_CAPITAL", "requestedAmount": 100000, "currency": "MYR",
       "monthlyCashAvailable": 30000, "monthlyDebtService": 12000}


def post(sim, side, build, body, **headers):
    return httpx.post(f"{sim[side]}/{build}/decisions", json=body,
                      headers={"X-API-Key": sim["api_key"], **headers}, timeout=5)


def register(sim, key, faults):
    r = httpx.put(f"{sim['dependency']}/control/scenarios", json={"scenarios": [{"key": key, "faults": faults}]},
                  headers={"X-Control-Key": sim["dep_control_key"]})
    assert r.status_code == 200


def test_response_contract(simulators):
    r = post(simulators, "baseline", "3.1.0", REQ)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"applicationId", "finalStatus", "probabilityOfDefault", "modelVersion",
                         "policyVersion", "reasonCodes", "traceId"}
    assert body["modelVersion"] == "3.1.0"


def test_traceparent_propagated(simulators):
    tid = "4bf92f3577b34da6a3ce929d0e0e4736"
    r = post(simulators, "baseline", "3.1.0", REQ, traceparent=f"00-{tid}-00f067aa0ba902b7-01")
    assert r.json()["traceId"] == tid


@pytest.mark.parametrize("mode", ["TIMEOUT", "UNAVAILABLE", "MALFORMED", "MISSING_RESPONSE"])
def test_bureau_faults_baseline_falls_back_candidate_defect_approves(simulators, mode):
    key = f"CT-BUR-{mode}"
    register(simulators, key, {"bureau": mode})
    body = {**REQ, "applicationId": key}
    base = post(simulators, "baseline", "3.1.0", body).json()
    cand = post(simulators, "candidate", "3.2.0", body).json()
    fixed = post(simulators, "candidate", "3.2.1", body).json()
    assert base["finalStatus"] == "MANUAL_REVIEW"
    assert cand["finalStatus"] == "APPROVED" and "BUREAU_DEFAULT_SCORE_APPLIED" in cand["reasonCodes"]
    assert fixed["finalStatus"] == "MANUAL_REVIEW"


def test_fraud_timeout_falls_back_to_review(simulators):
    register(simulators, "CT-FR-TO", {"fraud": "TIMEOUT"})
    assert post(simulators, "candidate", "3.2.0", {**REQ, "applicationId": "CT-FR-TO"}).json()["finalStatus"] == "MANUAL_REVIEW"


def test_api_level_faults(simulators):
    h = {"X-Control-Key": simulators["control_key"]}
    httpx.post(f"{simulators['candidate']}/control/api-faults", json={"mode": "malformed_json", "application_ids": ["CT-MAL"]}, headers=h)
    httpx.post(f"{simulators['candidate']}/control/api-faults", json={"mode": "http_500", "application_ids": ["CT-500"]}, headers=h)
    try:
        r = post(simulators, "candidate", "3.2.0", {**REQ, "applicationId": "CT-MAL"})
        with pytest.raises(ValueError):
            r.json()
        assert post(simulators, "candidate", "3.2.0", {**REQ, "applicationId": "CT-500"}).status_code == 500
    finally:
        httpx.delete(f"{simulators['candidate']}/control/api-faults", headers=h)


def test_control_endpoints_require_key(simulators):
    assert httpx.put(f"{simulators['dependency']}/control/scenarios", json={"scenarios": []}).status_code == 403
    assert httpx.post(f"{simulators['candidate']}/control/api-faults", json={"mode": "timeout"}).status_code == 403
