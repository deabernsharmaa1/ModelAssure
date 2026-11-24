"""Tenant isolation (QA-09), least privilege and audit of denials, tamper evidence."""
import pytest
from sqlalchemy import create_engine, text

from scripts.demo_setup import register_bank

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def bank(api, simulators):
    return register_bank(api.as_, simulators, system_name="Security Engine")


def test_other_org_cannot_read_project(api, bank):
    other = api.as_("admin@otherlender.example")
    assert other.get(f"/v1/projects/{bank['project']['id']}").status_code == 404
    assert bank["project"]["id"] not in [p["id"] for p in other.get("/v1/projects").json()]
    assert other.get(f"/v1/connections/{bank['connections']['3.1.0']['id']}").status_code == 404


def test_other_org_cannot_reference_foreign_version(api, bank):
    other = api.as_("admin@otherlender.example")
    r = other.post("/v1/connections", json={"system_version_id": bank["versions"]["3.1.0"]["id"], "endpoint": "http://localhost:9/x"})
    assert r.status_code in (403, 404)


def test_rls_enforced_in_database_even_without_app_checks(bank, database):
    app_url = database["owner_url"].replace("modelassure:modelassure", "modelassure_app:modelassure_app")
    eng = create_engine(app_url)
    other_org = database["orgs"]["Other Lender Sdn Bhd"]
    with eng.begin() as conn:
        conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": other_org})
        assert conn.execute(text("SELECT count(*) FROM projects")).scalar() == 0
        assert conn.execute(text("SELECT count(*) FROM connections")).scalar() == 0
    with eng.begin() as conn:  # no tenant at all → nothing visible
        assert conn.execute(text("SELECT count(*) FROM system_versions")).scalar() == 0
    with eng.begin() as conn:  # cannot write into another tenant
        conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": other_org})
        with pytest.raises(Exception, match="row-level security"):
            conn.execute(text("INSERT INTO systems (organization_id, name, category) VALUES (:o, 'x', 'SME_LENDING_DECISION')"),
                         {"o": bank["project"]["organization_id"]})


@pytest.mark.parametrize("who,method,url,body", [
    ("analyst", "post", "/v1/systems", {"name": "x"}),
    ("reviewer", "post", "/v1/projects", {}),
    ("auditor", "post", "/v1/systems", {"name": "y"}),
    ("analyst", "get", "/v1/audit-events", None),
])
def test_least_privilege(api, who, method, url, body):
    c = api.as_(who)
    r = c.post(url, json=body) if method == "post" else c.get(url)
    assert r.status_code == 403


def test_denials_are_audit_logged(api):
    api.as_("analyst").post("/v1/systems", json={"name": "denied-probe"})
    events = api.as_("auditor").get("/v1/audit-events", params={"action": "access.denied"}).json()["items"]
    assert any(e["details"]["permission"] == "system.manage" and e["actor_label"] == "analyst@tanjung.example"
               for e in events)


def test_app_role_cannot_rewrite_audit(bank, database):
    app_url = database["owner_url"].replace("modelassure:modelassure", "modelassure_app:modelassure_app")
    with create_engine(app_url).begin() as conn:
        conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": bank["project"]["organization_id"]})
        with pytest.raises(Exception, match="permission denied|append-only"):
            conn.execute(text("UPDATE audit_events SET action='x'"))


def test_tampering_detected_by_chain_verification(api, database):
    """Simulate a DBA editing an audit row behind the application's back."""
    other = api.as_("admin@otherlender.example")
    other.post("/v1/systems", json={"name": "Tamper Probe"})
    assert other.get("/v1/audit-events/verify").json()["valid"] is True
    org = database["orgs"]["Other Lender Sdn Bhd"]
    owner = create_engine(database["owner_url"])
    with owner.begin() as conn:
        conn.execute(text("ALTER TABLE audit_events DISABLE TRIGGER audit_events_append_only"))
        conn.execute(text("UPDATE audit_events SET details = '{\"forged\": true}' WHERE organization_id=:o AND seq=1"), {"o": org})
        conn.execute(text("ALTER TABLE audit_events ENABLE TRIGGER audit_events_append_only"))
    result = other.get("/v1/audit-events/verify").json()
    assert result["valid"] is False
    assert "event 1" in result["problem"]
