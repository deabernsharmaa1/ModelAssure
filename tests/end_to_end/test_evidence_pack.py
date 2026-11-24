"""Signed evidence packs: created, verified offline, tamper-evident, and attributable after key rotation."""
import base64
import io
import json
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from modules.evidence.verify_pack import main as verify_main
from modules.evidence.verify_pack import verify
from scripts.demo_setup import approve_policy_sets, queue_run, register_bank
from workers.executor import main as worker

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def run(api, simulators):
    bank = register_bank(api.as_, simulators, system_name="Evidence Pack Engine")
    for c in bank["connections"].values():
        api.as_("engineer").post(f"/v1/connections/{c['id']}/verify")
    policies = approve_policy_sets(api.as_, bank["project"]["id"])
    suite = api.as_("analyst").post("/v1/test-suites", json={
        "project_id": bank["project"]["id"], "name": "Pack suite",
        "config": {"seed": 11, "generators": [{"type": "normal"}, {"type": "missing_data"}, {"type": "cross_system_conflict"},
                                              {"type": "invalid_input"}]},
        "policy_set_ids": [policies["2026-06"]["id"], policies["2026-11"]["id"]]}).json()
    rid = queue_run(api.as_, bank, policies, suite, "3.2.0")["run_id"]
    worker.drain()
    r = api.as_("analyst").get(f"/v1/runs/{rid}").json()
    assert r["status"] == "COMPLETED"
    return r


def _pack(api, run_id, who="reviewer", **body):
    r = api.as_(who).post(f"/v1/runs/{run_id}/evidence-packs", json=body)
    assert r.status_code == 201, r.text
    out = r.json()
    dl = api.as_(who).get(f"/v1/runs/{run_id}/evidence-packs/{out['pack']['version']}")
    assert dl.status_code == 200 and dl.headers["X-Content-SHA256"] == out["pack"]["checksum"]
    return out, dl.content


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _rezip(data, change):
    """Copy a pack, letting `change(name, bytes)` replace (bytes), drop (None) or keep entries."""
    src, buf = zipfile.ZipFile(io.BytesIO(data)), io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in src.namelist():
            d = change(n, src.read(n))
            if d is not None:
                z.writestr(n, d)
    return buf, z


def test_pack_verifies_offline_and_matches_published_key(api, run, tmp_path):
    out, data = _pack(api, run["id"])
    keys = api.as_("analyst").get("/v1/signing-keys").json()
    active = next(k for k in keys if k["status"] == "ACTIVE")
    m = out["manifest"]
    assert m["signing"]["key_fingerprint"] == active["fingerprint"]
    assert m["run"]["assessment"] == "FAIL" and m["run"]["candidate_version"] == "3.2.0"

    path = _write(tmp_path, "pack.zip", data)
    r = verify(path, expect_fingerprint=active["fingerprint"])
    assert r["valid"], r["checks"]
    assert {c["check"] for c in r["checks"]} >= {"signature", "key_fingerprint", "expected_signer", "file_hashes",
                                                  "no_unlisted_files", "report_matches_manifest", "audit_event_hashes"}
    assert verify_main([path, "--expect-fingerprint", active["fingerprint"]]) == 0

    z = zipfile.ZipFile(io.BytesIO(data))
    names = set(z.namelist())
    assert {"report.json", "report.pdf", "outcomes.csv", "run_configuration.json", "findings.json", "reviews.json",
            "regulatory_mapping.json", "audit_events.json", "verify_pack.py", "VERIFY.md", "public_key.pem"} <= names
    assert not any(n.startswith("cases/") for n in names)  # no applicant-level evidence by default
    config = z.read("run_configuration.json").decode()
    assert "secret_ref" not in config and "local:" not in config and "http" not in config
    assert z.read("report.pdf")[:4] == b"%PDF"
    assert any(f["rule_ids"][:1] == ["POL-001"] for f in json.loads(z.read("findings.json")))
    actions = {e["payload"]["action"] for e in json.loads(z.read("audit_events.json"))}
    assert {"run.create", "report.generate"} <= actions
    assert z.read("verify_pack.py").startswith(b"#!/usr/bin/env python3")


def test_tampering_is_detected(api, run, tmp_path):
    _, data = _pack(api, run["id"])
    fp = api.as_("analyst").get("/v1/signing-keys").json()[0]["fingerprint"]

    def failed(buf_data, **kw):
        r = verify(_write(tmp_path, "t.zip", buf_data), **kw)
        assert not r["valid"]
        return {c["check"] for c in r["checks"] if not c["passed"]}

    # 1. A finding quietly removed from findings.json
    buf, _ = _rezip(data, lambda n, d: b"[]\n" if n == "findings.json" else d)
    assert failed(buf.getvalue()) == {"file_hashes"}

    # 2. Manifest edited to claim a PASS
    def edit_manifest(n, d):
        if n != "manifest.json":
            return d
        m = json.loads(d)
        m["run"]["assessment"] = "PASS"
        return json.dumps(m).encode()
    buf, _ = _rezip(data, edit_manifest)
    assert "signature" in failed(buf.getvalue())

    # 3. A file slipped in
    buf, _ = _rezip(data, lambda n, d: d)
    with zipfile.ZipFile(buf, "a") as z:
        z.writestr("cases/extra.json", b"{}")
    assert failed(buf.getvalue()) == {"no_unlisted_files"}

    # 4. Everything re-signed with someone else's key: internally consistent, wrong signer
    other = Ed25519PrivateKey.generate()
    other_pem = other.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    raw = other.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    import hashlib
    other_fp = hashlib.sha256(raw).hexdigest()
    src = zipfile.ZipFile(io.BytesIO(data))
    m = json.loads(src.read("manifest.json"))
    m["signing"]["key_fingerprint"] = other_fp
    m["run"]["assessment"] = "PASS"
    for f in m["files"]:
        if f["path"] == "public_key.pem":
            f["sha256"], f["bytes"] = hashlib.sha256(other_pem).hexdigest(), len(other_pem)
    mb = json.dumps(m).encode()
    replaced = {"manifest.json": mb, "manifest.sig": base64.b64encode(other.sign(mb)), "public_key.pem": other_pem}
    buf, _ = _rezip(data, lambda n, d: replaced.get(n, d))
    forged = buf.getvalue()
    assert verify(_write(tmp_path, "f.zip", forged))["valid"]  # consistent on its own…
    assert failed(forged, expect_fingerprint=fp) == {"expected_signer"}  # …but not from the issuer
    assert verify_main([_write(tmp_path, "f.zip", forged), "--expect-fingerprint", fp]) == 1
    assert verify_main([_write(tmp_path, "junk.zip", b"not a zip")]) == 2


def test_failing_case_evidence_option(api, run, tmp_path):
    out, data = _pack(api, run["id"], who="auditor", case_evidence="failing")
    counts = api.as_("analyst").get(f"/v1/runs/{run['id']}").json()["summary"]["by_result"]
    expected = sum(counts.get(k, 0) for k in ("FAIL", "ERROR", "INCONCLUSIVE"))
    z = zipfile.ZipFile(io.BytesIO(data))
    cases = [n for n in z.namelist() if n.startswith("cases/")]
    assert expected > 0 and len(cases) == expected == out["manifest"]["case_evidence"]["included"]
    assert "baseline" in json.loads(z.read(cases[0])) or "candidate" in json.loads(z.read(cases[0]))
    assert verify(_write(tmp_path, "c.zip", data))["valid"]


def test_who_may_issue_packs(api, run):
    for who in ("engineer", "analyst"):
        assert api.as_(who).post(f"/v1/runs/{run['id']}/evidence-packs", json={}).status_code == 403
        assert api.as_(who).get(f"/v1/runs/{run['id']}/evidence-packs/1").status_code == 403
    assert len(api.as_("analyst").get(f"/v1/runs/{run['id']}/evidence-packs").json()) >= 1  # metadata is visible
    assert api.as_("reviewer").post(f"/v1/runs/{run['id']}/evidence-packs", json={"case_evidence": "some"}).status_code == 422
    denied = api.as_("auditor").get("/v1/audit-events", params={"action": "access.denied"}).json()["items"]
    assert any(e["details"].get("permission") == "evidence_pack.create" for e in denied)


def test_key_rotation_keeps_old_packs_attributable(api, run, tmp_path):
    _, old_pack = _pack(api, run["id"])
    old_fp = next(k for k in api.as_("analyst").get("/v1/signing-keys").json() if k["status"] == "ACTIVE")["fingerprint"]
    assert api.as_("risk").post("/v1/signing-keys/rotate").status_code == 403
    r = api.as_("admin").post("/v1/signing-keys/rotate")
    assert r.status_code == 201 and r.json()["retired"]["fingerprint"] == old_fp
    new_fp = r.json()["active"]["fingerprint"]
    assert new_fp != old_fp
    keys = {k["fingerprint"]: k["status"] for k in api.as_("analyst").get("/v1/signing-keys").json()}
    assert keys[old_fp] == "RETIRED" and keys[new_fp] == "ACTIVE"
    assert verify(_write(tmp_path, "old.zip", old_pack), expect_fingerprint=old_fp)["valid"]
    new, new_pack = _pack(api, run["id"])
    assert new["manifest"]["signing"]["key_fingerprint"] == new_fp
    assert verify(_write(tmp_path, "new.zip", new_pack), expect_fingerprint=new_fp)["valid"]


def test_keys_and_packs_are_tenant_isolated(api, run):
    tanjung = {k["fingerprint"] for k in api.as_("analyst").get("/v1/signing-keys").json()}
    other = api.as_("admin@otherlender.example").get("/v1/signing-keys")
    assert other.status_code == 200 and not tanjung & {k["fingerprint"] for k in other.json()}
    assert api.as_("admin@otherlender.example").get(f"/v1/runs/{run['id']}/evidence-packs/1").status_code == 404
    assert api.as_("auditor").get("/v1/audit-events/verify").json()["valid"] is True


def test_signing_key_material_is_immutable_in_the_database(api, run, database):
    from sqlalchemy import create_engine, text

    _pack(api, run["id"])  # ensures an active key exists
    assert api.as_("admin").post("/v1/signing-keys/rotate").status_code == 201  # and a retired one
    app_url = database["owner_url"].replace("modelassure:modelassure", "modelassure_app:modelassure_app")
    org = run["organization_id"]
    for stmt in ("UPDATE signing_keys SET public_key_pem = 'x'", "UPDATE signing_keys SET secret_ref = 'local:x'",
                 "DELETE FROM signing_keys", "UPDATE signing_keys SET status = 'ACTIVE' WHERE status = 'RETIRED'"):
        with create_engine(app_url).begin() as conn:
            conn.execute(text("SELECT set_config('app.current_org', :o, true)"), {"o": org})
            assert conn.execute(text("SELECT count(*) FROM signing_keys")).scalar() >= 2
            with pytest.raises(Exception, match="immutable|cannot be deleted|permission denied"):
                conn.execute(text(stmt))
    with create_engine(app_url).begin() as conn:  # no tenant → no keys visible
        assert conn.execute(text("SELECT count(*) FROM signing_keys")).scalar() == 0
