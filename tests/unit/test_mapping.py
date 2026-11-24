import json
from pathlib import Path

import pytest

from modules.connectors.mapping import MappingError, build_request, normalize_response, validate_mapping

MAPPING = json.loads((Path(__file__).resolve().parents[2] / "decision_simulator/fixtures/tanjung_mapping.json").read_text())


def test_request_mapping_renames_nested_fields():
    req = build_request(MAPPING, {"case_id": "C1", "requested_amount": 5, "fraud": {"status": "HIGH", "alert_resolved": True}})
    assert req == {"applicationId": "C1", "requestedAmount": 5, "fraud": {"status": "HIGH", "alertResolved": True}}


def test_request_mapping_omits_missing_and_keeps_invalid_values_verbatim():
    req = build_request(MAPPING, {"case_id": "C1", "requested_amount": "not-a-number"})
    assert req == {"applicationId": "C1", "requestedAmount": "not-a-number"}


def test_non_object_input_passed_through():
    assert build_request(MAPPING, [1, 2]) == [1, 2]


def test_response_normalisation_and_transformation_log():
    raw = {"applicationId": "C1", "finalStatus": "MANUAL_REVIEW", "probabilityOfDefault": 0.1,
           "modelVersion": "3.2.0", "policyVersion": "2026-11", "reasonCodes": ["X"], "traceId": "t"}
    out, errors, log = normalize_response(MAPPING, raw)
    assert errors == []
    assert out["decision"] == "REVIEW" and out["model_version"] == "3.2.0"
    assert {"field": "decision", "source": "finalStatus", "raw": "MANUAL_REVIEW", "normalized": "REVIEW"} in log


def test_unmapped_decision_is_a_schema_error():
    out, errors, _ = normalize_response(MAPPING, {"finalStatus": "PENDING_SOMETHING"})
    assert out is None and "unmapped value" in errors[0]


def test_non_object_body_is_a_schema_error():
    out, errors, _ = normalize_response(MAPPING, ["x"])
    assert out is None and errors


def test_pd_out_of_range_is_a_schema_error():
    out, errors, _ = normalize_response(None, {"decision": "APPROVE", "pd": 3})
    assert out is None and "pd" in errors[0]


def test_identity_mapping():
    out, errors, _ = normalize_response(None, {"decision": "HOLD", "model_version": "1"})
    assert errors == [] and out["decision"] == "HOLD"


@pytest.mark.parametrize("bad", [
    {"response": {"fields": {"not_canonical": {"source": "x"}}}},
    {"response": {"fields": {"decision": {"source": "s", "values": {"A": "APPROVED"}}}}},
    {"request": {"fields": {"a": 1}}},
    {"other": {}},
])
def test_invalid_mappings_rejected(bad):
    with pytest.raises(MappingError):
        validate_mapping(bad)


def test_demo_mapping_valid():
    validate_mapping(MAPPING)
