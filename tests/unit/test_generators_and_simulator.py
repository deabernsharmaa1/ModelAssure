"""Scenario generation determinism and the fictional bank's seeded behaviour (in-process)."""
import collections
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from common.hashing import content_hash
from decision_simulator.app_factory import API_KEY, create_app
from modules.connectors.mapping import build_request, normalize_response
from modules.evaluation.engine import Observation, evaluate_case
from modules.policies.policy_set import compile_policy_set
from modules.scenarios.generators import generate_suite

FIX = Path(__file__).resolve().parents[2] / "decision_simulator" / "fixtures"
BASE = compile_policy_set(json.loads((FIX / "policy_set_2026-06.json").read_text()))
CAND = compile_policy_set(json.loads((FIX / "policy_set_2026-11.json").read_text()))
MAPPING = json.loads((FIX / "tanjung_mapping.json").read_text())
STANDARD = json.loads((Path(__file__).resolve().parents[2] / "test_suites/lending/standard_release_suite.json").read_text())


@pytest.fixture(scope="module")
def suite():
    return generate_suite(STANDARD, [BASE, CAND])


def test_generation_is_deterministic(suite):
    again = generate_suite(STANDARD, [BASE, CAND])
    assert content_hash(suite) == content_hash(again)


def test_suite_has_at_least_1000_cases_and_unique_keys(suite):
    assert len(suite) >= 1000
    assert len({c["case_key"] for c in suite}) == len(suite)
    ids = [c["input"]["case_id"] for c in suite if isinstance(c["input"], dict) and "case_id" in c["input"]]
    assert len(ids) == len(set(ids))


def test_all_mvp_categories_present(suite):
    cats = {c["category"] for c in suite}
    assert {"NORMAL", "BOUNDARY", "MISSING_DATA", "DEPENDENCY_FAILURE", "CROSS_SYSTEM_CONFLICT",
            "VERSION_REGRESSION", "DUPLICATE", "INVALID_INPUT"} <= cats


def test_boundary_values_straddle_both_dscr_limits(suite):
    tags = {t for c in suite if c["category"] == "BOUNDARY" for t in c["tags"]}
    for v in ("1.24", "1.25", "1.26", "1.49", "1.5", "1.51"):
        assert f"derived.dscr={float(v)}" in tags


# ---- the fictional bank, driven through its real HTTP app (no dependency fetches needed) ----
baseline_client = TestClient(create_app(["3.1.0"], "baseline"))
candidate_client = TestClient(create_app(["3.2.0", "3.2.1"], "candidate"))


def call(client, build, case):
    r = client.post(f"/{build}/decisions", json=build_request(MAPPING, case["input"]), headers={"X-API-Key": API_KEY})
    if 400 <= r.status_code < 500:
        return Observation(status="REJECTED", http_status=r.status_code)
    out, errors, _ = normalize_response(MAPPING, r.json())
    assert not errors, errors
    return Observation(status="OK", http_status=200, normalized=out)


def evaluate(suite, build):
    counts, violations = collections.Counter(), collections.Counter()
    for case in suite:
        if case["category"] in ("DEPENDENCY_FAILURE",):
            continue  # needs the live dependency simulator (covered by contract tests)
        b, c = call(baseline_client, "3.1.0", case), call(candidate_client, build, case)
        r = evaluate_case(case_key=case["case_key"], category=case["category"], case_input=case["input"],
                          scenario=case["scenario"], expected_behavior=case["expected_behavior"],
                          baseline=b, candidate=c, candidate_policies=CAND, baseline_policies=BASE)
        counts[r.state] += 1
        for v in r.violations:
            violations[v["policy_id"] or v["failure_type"]] += 1
        assert not r.baseline_violations, (case["case_key"], r.baseline_violations)
    return counts, violations


@pytest.fixture(scope="module")
def results(suite):
    small = [c for c in suite if c["category"] != "VERSION_REGRESSION"] + \
            [c for c in suite if c["category"] == "VERSION_REGRESSION"][:200]
    return {b: evaluate(small, b) for b in ("3.2.0", "3.2.1")}


def test_defective_candidate_shows_seeded_defects(results):
    counts, violations = results["3.2.0"]
    assert violations["POL-001"] > 0 and violations["POL-002"] > 0 and violations["CHG-002"] > 0
    assert counts["ERROR"] == 0 and counts["INCONCLUSIVE"] == 0


def test_fixed_candidate_has_no_violations(results):
    counts, violations = results["3.2.1"]
    assert sum(violations.values()) == 0, violations
    assert counts["FAIL"] == 0 and counts["CHANGED"] > 0  # approved DSCR change still visible


def test_simulator_rejects_invalid_input_safely():
    r = baseline_client.post("/3.1.0/decisions", json={"applicationId": "X", "requestedAmount": "1"},
                             headers={"X-API-Key": API_KEY})
    assert r.status_code == 422


def test_simulator_requires_api_key():
    assert baseline_client.post("/3.1.0/decisions", json={}).status_code == 401


@pytest.mark.parametrize("key", ["../../escape", "a/b", "x" * 101, "bad key", ".hidden"])
def test_unsafe_fixture_case_keys_rejected(key):
    from modules.scenarios.generators import GenerationError

    cfg = {"generators": [{"type": "fixtures", "cases": [{"case_key": key, "input": {"case_id": "c"}}]}]}
    with pytest.raises(GenerationError):
        generate_suite(cfg, [])


def test_production_refuses_development_settings():
    from main import _check_production_safety
    from settings import Settings

    with pytest.raises(RuntimeError, match="MA_AUTH_MODE"):
        _check_production_safety(Settings(environment="production", auth_mode="dev"))
    _check_production_safety(Settings(environment="production", auth_mode="oidc",
                                      jwt_secret="a-real-secret-value-from-a-vault-123456",
                                      storage_encryption_key="k"))
