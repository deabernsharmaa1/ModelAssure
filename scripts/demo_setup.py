"""Registers the fictional bank in ModelAssure through the public API, exactly as a customer's
team would. Used by the end-to-end demo script and by the integration tests.

`as_(role)` must return an object with get/post/patch methods taking (url, json=...) and
returning a response with .status_code and .json().
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "decision_simulator" / "fixtures"


def _ok(r, expected=(200, 201)) -> Any:
    if r.status_code not in expected:
        raise RuntimeError(f"{r.status_code}: {r.text}")
    return r.json()


def register_bank(as_: Callable[[str], Any], sim: dict[str, str], system_name: str = "Tanjung SME Decision Engine") -> dict[str, Any]:
    eng, risk = as_("engineer"), as_("risk")
    mapping = json.loads((FIX / "tanjung_mapping.json").read_text())

    system = _ok(eng.post("/v1/systems", json={"name": system_name, "category": "SME_LENDING_DECISION",
                                               "vendor": "Tanjung Kredit (fictional)"}))
    versions = {}
    for v, model in (("3.1.0", "3.1.0"), ("3.2.0", "3.2.0"), ("3.2.1", "3.2.1")):
        versions[v] = _ok(eng.post(f"/v1/systems/{system['id']}/versions", json={
            "version": v, "expected_model_version": model,
            "description": {"3.1.0": "Current production release", "3.2.0": "Release candidate (Nov 2026)",
                            "3.2.1": "Release candidate with engineering fixes"}[v]}))
    connections = {}
    for v, side in (("3.1.0", "baseline"), ("3.2.0", "candidate"), ("3.2.1", "candidate")):
        connections[v] = _ok(eng.post("/v1/connections", json={
            "system_version_id": versions[v]["id"], "kind": "REST",
            "endpoint": f"{sim[side]}/{v}/decisions", "auth_header": "X-API-Key", "credential": sim["api_key"],
            "mapping": mapping, "timeout_ms": int(sim.get("timeout_ms", 3000)), "max_concurrency": 5,
            "dependency_control_url": sim["dependency"], "dependency_credential": sim["dep_control_key"],
        }))
    users = {u["email"]: u for u in _ok(risk.get("/v1/users"))}
    project = _ok(risk.post("/v1/projects", json={
        "name": f"SME Lending v3.2 release — {system_name}",
        "business_use_case": "Automated SME working-capital lending decisions (credit, fraud, bureau, identity).",
        "system_id": system["id"], "owner_id": users["risk@tanjung.example"]["id"],
        "baseline_version_id": versions["3.1.0"]["id"], "candidate_version_id": versions["3.2.0"]["id"],
        "risk_classification": "HIGH", "test_environment": "Fictional bank sandbox (simulator)",
    }))
    return {"system": system, "versions": versions, "connections": connections, "project": project, "users": users}
