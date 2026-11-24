"""Phase 2: identity, projects, version registry, connections, audit trail."""
import json

import pytest
from sqlalchemy import create_engine, text

from scripts.demo_setup import FIX, register_bank

MAPPING = json.loads((FIX / "tanjung_mapping.json").read_text())

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def bank(api, simulators):
    return register_bank(api.as_, simulators, system_name="Phase2 Engine")


def test_login_and_me(api):
    me = api.as_("risk").get("/v1/me").json()
    assert me["roles"] == ["RISK_MANAGER"] and me["organization"]["name"] == "Tanjung Kredit Berhad"


def test_wrong_password_rejected(api):
    r = api.client.post("/v1/auth/login", json={"email": "risk@tanjung.example", "password": "nope"})
    assert r.status_code == 401


def test_no_token_rejected(api):
    assert api.client.get("/v1/projects").status_code == 401


def test_bank_registration(bank):
    assert set(bank["versions"]) == {"3.1.0", "3.2.0", "3.2.1"}
    assert bank["project"]["risk_classification"] == "HIGH"
    for c in bank["connections"].values():
        assert "credential" not in c and "secret_ref" not in c and c["has_credential"] is True


def test_versions_are_immutable(api, bank, database):
    r = api.as_("engineer").post(f"/v1/systems/{bank['system']['id']}/versions", json={"version": "3.2.0"})
    assert r.status_code == 409
    eng = create_engine(database["owner_url"].replace("modelassure:modelassure", "modelassure_app:modelassure_app"))
    with eng.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": bank["project"]["organization_id"]})
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("UPDATE system_versions SET version='hacked' WHERE id=:i"), {"i": bank["versions"]["3.2.0"]["id"]})


def test_connection_verification_passes(api, bank):
    for v, c in bank["connections"].items():
        r = api.as_("engineer").post(f"/v1/connections/{c['id']}/verify").json()
        assert r["status"] == "VERIFIED", r
        assert any(ch["check"] == "auth_enforced" and ch["passed"] for ch in r["checks"])


def test_connection_verification_detects_wrong_version(api, bank, simulators):
    # Register 3.2.1's endpoint against a version that expects a different model version.
    eng = api.as_("engineer")
    v = eng.post(f"/v1/systems/{bank['system']['id']}/versions", json={"version": "9.9.9", "expected_model_version": "9.9.9"}).json()
    c = eng.post("/v1/connections", json={"system_version_id": v["id"], "endpoint": f"{simulators['candidate']}/3.2.1/decisions",
                                          "auth_header": "X-API-Key", "credential": simulators["api_key"],
                                          "mapping": MAPPING}).json()
    r = eng.post(f"/v1/connections/{c['id']}/verify").json()
    assert r["status"] == "FAILED", r
    assert any(ch["check"] == "reported_version" and not ch["passed"] for ch in r["checks"]), r["checks"]


def test_connection_verification_detects_bad_credential(api, bank, simulators):
    eng = api.as_("engineer")
    c = eng.post("/v1/connections", json={"system_version_id": bank["versions"]["3.1.0"]["id"],
                                          "endpoint": f"{simulators['baseline']}/3.1.0/decisions",
                                          "auth_header": "X-API-Key", "credential": "wrong", "mapping": MAPPING}).json()
    r = eng.post(f"/v1/connections/{c['id']}/verify").json()
    assert r["status"] == "FAILED" and any(ch["check"] == "authentication" for ch in r["checks"])


@pytest.mark.parametrize("endpoint", [
    "http://169.254.169.254/latest/meta-data",
    "http://metadata.google.internal/computeMetadata",
    "https://prod-lending.bank.example/decide",
    "ftp://localhost/x",
    "http://user:pass@localhost:9/x",
])
def test_egress_policy_blocks_unapproved_destinations(api, bank, endpoint):
    r = api.as_("engineer").post("/v1/connections", json={"system_version_id": bank["versions"]["3.1.0"]["id"], "endpoint": endpoint})
    assert r.status_code == 422, r.text


def test_production_environment_cannot_be_registered(api, bank, simulators):
    r = api.as_("engineer").post("/v1/connections", json={"system_version_id": bank["versions"]["3.1.0"]["id"],
                                                          "endpoint": f"{simulators['baseline']}/3.1.0/decisions",
                                                          "environment": "PRODUCTION"})
    assert r.status_code == 422


def test_audit_chain_records_and_verifies(api, bank):
    aud = api.as_("auditor")
    events = aud.get("/v1/audit-events", params={"action": "connection.create"}).json()
    assert events["total"] >= 3
    assert aud.get("/v1/audit-events/verify").json()["valid"] is True
