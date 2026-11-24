"""CI/CD release gate: pipeline keys, gate runs, exit codes, segregation of duties, and the CLI."""
import threading

import pytest
from sqlalchemy import create_engine, text

from scripts.demo_setup import approve_policy_sets, queue_run, register_bank
from scripts.modelassure_gate import main as gate_cli
from scripts.sim_processes import free_port, start_uvicorn
from workers.executor import main as worker

pytestmark = pytest.mark.integration

SUITE = {"seed": 5, "generators": [{"type": "normal"}, {"type": "missing_data"}, {"type": "cross_system_conflict"},
                                   {"type": "invalid_input"}]}


class KeyClient:
    """Calls the API with a pipeline key instead of a user token."""

    def __init__(self, client, key):
        self.c, self.h = client, {"Authorization": f"Bearer {key}"}

    def get(self, url, **kw):
        return self.c.get(url, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def post(self, url, **kw):
        return self.c.post(url, headers={**self.h, **kw.pop("headers", {})}, **kw)


@pytest.fixture(scope="module")
def setup(api, simulators):
    bank = register_bank(api.as_, simulators, system_name="Gate Engine")
    for v in ("3.1.0", "3.2.0"):  # 3.2.1 deliberately left unverified: the gate must verify it
        api.as_("engineer").post(f"/v1/connections/{bank['connections'][v]['id']}/verify")
    policies = approve_policy_sets(api.as_, bank["project"]["id"])
    suite = api.as_("analyst").post("/v1/test-suites", json={
        "project_id": bank["project"]["id"], "name": "Gate suite", "config": SUITE,
        "policy_set_ids": [policies["2026-06"]["id"], policies["2026-11"]["id"]]}).json()
    queue_run(api.as_, bank, policies, suite, "3.2.0")
    worker.drain()
    created = api.as_("admin").post("/v1/api-keys", json={"name": "GitHub Actions", "project_id": bank["project"]["id"]})
    assert created.status_code == 201, created.text
    return {"bank": bank, "project_id": bank["project"]["id"], "key": created.json()["key"], "key_row": created.json(),
            "ci": KeyClient(api.client, created.json()["key"])}


def _gate(setup, **body):
    return setup["ci"].post("/v1/gate/runs", json={"project_id": setup["project_id"], **body})


def _finish(setup, run_id):
    worker.drain()
    r = setup["ci"].get(f"/v1/gate/runs/{run_id}")
    assert r.status_code == 200 and r.json()["finished"], r.text
    return r.json()


def test_keys_with_underscores_in_the_secret_authenticate(api, setup):
    """token_urlsafe secrets often contain '_'; parsing must not depend on the secret's characters."""
    keys = [api.as_("admin").post("/v1/api-keys", json={"name": f"k{i}", "project_id": setup["project_id"]}).json()["key"]
            for i in range(12)]
    assert any("_" in k.split("_", 2)[2] for k in keys)
    assert all(KeyClient(api.client, k).get("/v1/me").status_code == 200 for k in keys)


def test_key_is_shown_once_and_only_admins_manage_keys(api, setup):
    assert setup["key"].startswith("mak_") and setup["key_row"]["status"] == "ACTIVE"
    listed = api.as_("admin").get("/v1/api-keys").json()
    assert any(k["key_id"] == setup["key_row"]["key_id"] for k in listed)
    assert setup["key"] not in str(listed) and all("secret" not in k for k in listed)
    assert api.as_("risk").post("/v1/api-keys", json={"name": "x", "project_id": setup["project_id"]}).status_code == 403
    assert api.as_("engineer").get("/v1/api-keys").status_code == 403
    # Pipeline identities don't appear as people (e.g. owner pickers)
    assert not any(u["email"].endswith("service.modelassure.invalid") for u in api.as_("risk").get("/v1/users").json())


def test_key_can_only_use_the_gate_for_its_project(api, setup):
    ci = setup["ci"]
    assert ci.get("/v1/me").json()["roles"] == ["CI"]
    assert ci.get(f"/v1/projects/{setup['project_id']}").status_code == 403
    assert ci.get("/v1/findings").status_code == 403
    assert ci.post("/v1/runs", json={}).status_code == 403
    assert ci.post("/v1/api-keys", json={"name": "x", "project_id": setup["project_id"]}).status_code == 403
    b = setup["bank"]
    foreign = api.as_("risk").post("/v1/projects", json={
        "name": "Another release", "business_use_case": "Scope test", "system_id": b["system"]["id"],
        "owner_id": b["users"]["risk@tanjung.example"]["id"], "baseline_version_id": b["versions"]["3.1.0"]["id"],
        "candidate_version_id": b["versions"]["3.2.0"]["id"], "risk_classification": "LOW", "test_environment": "Simulator"})
    assert foreign.status_code == 201, foreign.text
    r = ci.post("/v1/gate/runs", json={"project_id": foreign.json()["id"], "candidate_version": "9.9.9"})
    assert r.status_code == 403 and "scoped" in r.json()["error"]["message"]
    assert ci.get(f"/v1/gate/runs/{setup['bank']['project']['id']}").status_code in (403, 404)
    for bad in ("mak_nothex00000_" + "x" * 40, "mak_0123456789ab_" + "x" * 40, "mak_short"):
        assert KeyClient(api.client, bad).get("/v1/me").status_code == 401


def test_gate_fails_the_defective_build(setup):
    r = _gate(setup, candidate_version="3.2.0", source={"commit": "abc123"})
    assert r.status_code == 202, r.text
    res = _finish(setup, r.json()["run_id"])
    assert res["outcome"] == "FAIL" and res["exit_code"] == 1
    assert any("POL-001" in f or "bureau" in f.lower() for f in res["blocking_findings"])
    assert res["release_review_url"].endswith(f"/release?run={res['run_id']}")
    assert "not a release authorisation" in res["statement"]


def test_gate_verifies_and_passes_the_fixed_build(api, setup, simulators):
    # 3.2.1 is registered but its connection was never verified: the gate clones the last
    # candidate connection with the new endpoint and verifies it before running.
    r = _gate(setup, candidate_version="3.2.1", candidate_endpoint=f"{simulators['candidate']}/3.2.1/decisions")
    assert r.status_code == 202, r.text
    res = _finish(setup, r.json()["run_id"])
    assert res["outcome"] == "PASS" and res["exit_code"] == 0, res
    assert res["candidate_version"] == "3.2.1" and res["baseline_version"] == "3.1.0"
    setup["pass_run"] = res["run_id"]
    events = api.as_("auditor").get("/v1/audit-events", params={"action": "gate.start"}).json()["items"]
    assert any(e["actor_label"].endswith("service.modelassure.invalid") for e in events)


def test_gate_registers_a_new_version(api, setup, simulators):
    r = _gate(setup, candidate_version="3.2.1-build.7", expected_model_version="3.2.1",
              candidate_endpoint=f"{simulators['candidate']}/3.2.1/decisions", description="commit 7f3e")
    assert r.status_code == 202, r.text
    res = _finish(setup, r.json()["run_id"])
    assert res["exit_code"] == 0
    versions = api.as_("analyst").get(f"/v1/systems/{setup['bank']['system']['id']}/versions").json()
    assert any(v["version"] == "3.2.1-build.7" and v["expected_model_version"] == "3.2.1" for v in versions)


def test_gate_rejects_bad_endpoints(setup, simulators):
    wrong = _gate(setup, candidate_version="4.0.0", candidate_endpoint=f"{simulators['candidate']}/3.2.0/decisions")
    assert wrong.status_code == 422 and wrong.json()["error"]["code"] == "CONNECTION_NOT_VERIFIED"  # reports 3.2.0, not 4.0.0
    outside = _gate(setup, candidate_version="4.0.1", candidate_endpoint="https://example.com/decisions")
    assert outside.status_code == 422 and "not permitted" in outside.json()["error"]["message"]
    missing = _gate(setup, candidate_version="4.0.2")
    assert missing.status_code == 422 and "candidate_endpoint" in missing.json()["error"]["message"]
    assert _gate(setup, candidate_version="3.1.0").status_code == 422  # the baseline itself


def test_gate_is_idempotent(setup, simulators):
    body = {"candidate_version": "3.2.1", "candidate_endpoint": f"{simulators['candidate']}/3.2.1/decisions"}
    def call():
        return setup["ci"].post("/v1/gate/runs", json={"project_id": setup["project_id"], **body},
                                headers={"Idempotency-Key": "pipeline-42"})
    a, b = call(), call()
    assert a.status_code == b.status_code == 202 and a.json()["run_id"] == b.json()["run_id"]
    worker.drain()


def test_key_creator_cannot_review_what_the_pipeline_ran(api, setup, database):
    """Segregation of duties follows the key to the person who created it."""
    admin_id = next(u["id"] for u in api.as_("risk").get("/v1/users").json() if u["email"] == "admin@tanjung.example")
    org = setup["bank"]["project"]["organization_id"]
    with create_engine(database["owner_url"]).begin() as conn:
        conn.execute(text("INSERT INTO memberships (user_id, organization_id, role) VALUES (:u, :o, 'REVIEWER') "
                          "ON CONFLICT DO NOTHING"), {"u": admin_id, "o": org})
    run_id = setup["pass_run"]
    denied = api.as_("admin").post(f"/v1/runs/{run_id}/reviews", json={"decision": "APPROVE_RELEASE",
                                                                         "rationale": "Pipeline passed, approving."})
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "SEGREGATION_OF_DUTIES"
    assert "initiated the test run" in denied.json()["error"]["message"]
    ok = api.as_("reviewer").post(f"/v1/runs/{run_id}/reviews", json={"decision": "APPROVE_RELEASE",
                                                                        "rationale": "Gate run passed; findings reviewed."})
    assert ok.status_code == 201, ok.text


def test_cli_exit_codes_against_a_live_api(setup, simulators):
    port = free_port()
    proc = start_uvicorn("main:app", port)
    stop = threading.Event()

    def work():
        while not stop.is_set():
            if not worker.run_once():
                stop.wait(0.2)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    try:
        import os
        os.environ["MODELASSURE_API_KEY"] = setup["key"]
        base = ["--api", f"http://127.0.0.1:{port}", "--project", setup["project_id"], "--poll", "2", "--timeout", "120"]
        assert gate_cli([*base, "--version", "3.2.0", "--commit", "cli-1"]) == 1
        assert gate_cli([*base, "--version", "3.2.1", "--endpoint", f"{simulators['candidate']}/3.2.1/decisions",
                         "--commit", "cli-2"]) == 0
        assert gate_cli([*base, "--version", "5.0.0", "--endpoint", f"{simulators['candidate']}/3.2.0/decisions"]) == 3
        os.environ["MODELASSURE_API_KEY"] = "mak_0123456789ab_" + "x" * 40
        assert gate_cli([*base, "--version", "3.2.1"]) == 3
    finally:
        os.environ.pop("MODELASSURE_API_KEY", None)
        stop.set()
        t.join(timeout=30)
        proc.terminate()
        proc.wait(timeout=10)


def test_revoked_key_stops_working_and_identity_is_immutable(api, setup, database):
    kid = setup["key_row"]["id"]
    assert api.as_("admin").post(f"/v1/api-keys/{kid}/revoke").json()["status"] == "REVOKED"
    r = setup["ci"].get("/v1/me")
    assert r.status_code == 401 and "revoked" in r.json()["error"]["message"]
    assert api.as_("admin").post(f"/v1/api-keys/{kid}/revoke").status_code == 409
    app_url = database["owner_url"].replace("modelassure:modelassure", "modelassure_app:modelassure_app")
    for stmt in ("UPDATE api_keys SET revoked_at = NULL", "UPDATE api_keys SET secret_hash = 'x'",
                 "UPDATE api_keys SET expires_at = now() + interval '10 years'", "DELETE FROM api_keys"):
        with create_engine(app_url).begin() as conn:
            conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": setup["bank"]["project"]["organization_id"]})
            with pytest.raises(Exception, match="immutable|cannot be deleted|permission denied"):
                conn.execute(text(stmt))
    with create_engine(app_url).begin() as conn:  # without a tenant the table is empty to the app role
        assert conn.execute(text("SELECT count(*) FROM api_keys")).scalar() == 0
    events = {e["action"] for e in api.as_("auditor").get("/v1/audit-events", params={"resource_type": "api_key"}).json()["items"]}
    assert {"api_key.create", "api_key.revoke"} <= events
    assert api.as_("auditor").get("/v1/audit-events/verify").json()["valid"] is True
