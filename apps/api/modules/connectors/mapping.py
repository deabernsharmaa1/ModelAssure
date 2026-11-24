"""Field mapping between the canonical decision schema and a customer's API (FR-07).

A mapping document:

{
  "request": {                       # customer field  <-  canonical field
    "fields": {"applicationId": "case_id", "requestedAmount": "requested_amount",
               "fraud.alertResolved": "fraud.alert_resolved", ...}
  },
  "response": {                      # canonical field <-  customer field
    "fields": {
      "case_id":        {"source": "applicationId"},
      "decision":       {"source": "finalStatus",
                         "values": {"APPROVED": "APPROVE", "MANUAL_REVIEW": "REVIEW", ...}},
      "pd":             {"source": "probabilityOfDefault"},
      "model_version":  {"source": "modelVersion"},
      "policy_version": {"source": "policyVersion"},
      "reason_codes":   {"source": "reasonCodes"},
      "trace_id":       {"source": "traceId"}
    }
  }
}

An empty / missing mapping means the customer already speaks the canonical schema.
Every transformation is recorded so the audit trail shows exactly how a raw value became the
normalised value that was judged (spec §7: "preserving the source fields and transformations").
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

from modules.policies.dsl import DECISIONS, MISSING

CANONICAL_OUTPUT_FIELDS = ("case_id", "decision", "pd", "model_version", "policy_version", "reason_codes", "trace_id")
VOLATILE_OUTPUT_FIELDS = frozenset({"trace_id"})  # excluded from comparisons and hashes of decisions


class MappingError(ValueError):
    pass


def _get(obj: Any, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


def _set(obj: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def validate_mapping(mapping: dict[str, Any] | None) -> dict[str, Any]:
    mapping = mapping or {}
    if not isinstance(mapping, dict):
        raise MappingError("mapping must be an object")
    extra = set(mapping) - {"request", "response"}
    if extra:
        raise MappingError(f"unsupported mapping keys {sorted(extra)}")
    req = (mapping.get("request") or {}).get("fields") or {}
    if not isinstance(req, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in req.items()):
        raise MappingError("request.fields must map customer field -> canonical field (strings)")
    resp = (mapping.get("response") or {}).get("fields") or {}
    if not isinstance(resp, dict):
        raise MappingError("response.fields must be an object")
    for canon, spec in resp.items():
        if canon not in CANONICAL_OUTPUT_FIELDS:
            raise MappingError(f"response field '{canon}' is not in the canonical schema {CANONICAL_OUTPUT_FIELDS}")
        if not isinstance(spec, dict) or not isinstance(spec.get("source"), str):
            raise MappingError(f"response.fields.{canon} needs a 'source' string")
        values = spec.get("values")
        if values is not None:
            if not isinstance(values, dict):
                raise MappingError(f"response.fields.{canon}.values must be an object")
            if canon == "decision":
                bad = [v for v in values.values() if v not in DECISIONS]
                if bad:
                    raise MappingError(f"decision values must map to {list(DECISIONS)}; got {bad}")
    return mapping


def build_request(mapping: dict[str, Any] | None, canonical_input: Any) -> Any:
    """Translate a canonical input into the customer's request body.

    Values are copied verbatim (including deliberately invalid ones used by invalid-input tests).
    Non-object inputs (e.g. a malformed-payload test) are passed through unchanged."""
    fields = ((mapping or {}).get("request") or {}).get("fields") or {}
    if not fields or not isinstance(canonical_input, dict):
        return canonical_input
    out: dict[str, Any] = {}
    for customer_field, canon_field in fields.items():
        v = _get(canonical_input, canon_field)
        if v is not MISSING:
            _set(out, customer_field, v)
    return out


class NormalizedDecision(BaseModel):
    """The common decision schema every response must satisfy (FR-08 response validation)."""
    case_id: str | None = None
    decision: str
    pd: float | None = Field(default=None, ge=0.0, le=1.0)
    model_version: str | None = None
    policy_version: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    trace_id: str | None = None

    @field_validator("decision")
    @classmethod
    def _decision_known(cls, v: str) -> str:
        if v not in DECISIONS:
            raise ValueError(f"decision '{v}' is not one of {list(DECISIONS)}")
        return v


def normalize_response(mapping: dict[str, Any] | None, raw: Any) -> tuple[dict[str, Any] | None, list[str], list[dict[str, Any]]]:
    """Returns (normalised output or None, schema errors, transformation log)."""
    errors: list[str] = []
    log: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        return None, [f"response body is not a JSON object (got {type(raw).__name__})"], log
    fields = ((mapping or {}).get("response") or {}).get("fields") or {}
    candidate: dict[str, Any] = {}
    if not fields:
        candidate = {k: raw[k] for k in CANONICAL_OUTPUT_FIELDS if k in raw}
    else:
        for canon, spec in fields.items():
            v = _get(raw, spec["source"])
            if v is MISSING:
                continue
            values = spec.get("values")
            if values is not None:
                if isinstance(v, str) and v in values:
                    log.append({"field": canon, "source": spec["source"], "raw": v, "normalized": values[v]})
                    v = values[v]
                else:
                    errors.append(f"unmapped value {v!r} for '{spec['source']}' (→ {canon})")
                    continue
            elif spec["source"] != canon:
                log.append({"field": canon, "source": spec["source"], "raw": v, "normalized": v})
            candidate[canon] = v
    if "decision" not in candidate and not errors:
        errors.append("response has no decision field")
    if errors:
        return None, errors, log
    try:
        model = NormalizedDecision.model_validate(candidate)
    except ValidationError as e:
        return None, [f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()], log
    return model.model_dump(), [], log


def comparable(output: dict[str, Any] | None) -> dict[str, Any] | None:
    if output is None:
        return None
    return {k: v for k, v in output.items() if k not in VOLATILE_OUTPUT_FIELDS}
