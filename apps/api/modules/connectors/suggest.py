"""Mapping helper: proposes a field mapping from sample request and response payloads (FR-07).

The suggestion is a draft for an engineer to review. It never guesses silently:
  * every proposed field says how it was matched (exact name or known synonym);
  * a field whose sample value has the wrong shape (e.g. a boolean `fraudFlag` where the canonical
    `fraud.status` is a status string) is reported, not mapped, because request mapping copies
    values verbatim;
  * only decision values seen in the samples are mapped. Any other value the system later returns
    is reported as a technical ERROR during a run, never as a pass.
"""
from __future__ import annotations

import re
from typing import Any

from modules.connectors.mapping import MappingError, normalize_response, validate_mapping
from modules.policies.dsl import DECISIONS

# canonical input field -> (expected type, synonyms)
INPUT_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "case_id": ("string", ("caseId", "applicationId", "appId", "applicationRef", "applicationReference", "referenceId", "requestRef")),
    "product": ("string", ("productCode", "productType", "productId")),
    "requested_amount": ("number", ("requestedAmount", "loanAmount", "amount", "financingAmount", "amountRequested", "facilityAmount")),
    "currency": ("string", ("ccy", "currencyCode")),
    "monthly_cash_available": ("number", ("monthlyCashAvailable", "monthlyCashflow", "monthlyCashFlow", "cashAvailable", "netMonthlyCashflow")),
    "monthly_debt_service": ("number", ("monthlyDebtService", "debtService", "monthlyRepayment", "monthlyRepayments")),
    "requested_instalment": ("number", ("requestedInstalment", "instalment", "installment", "monthlyInstalment", "monthlyInstallment")),
    "monthly_net_income": ("number", ("monthlyNetIncome", "netIncome", "netMonthlyIncome", "monthlyIncome")),
    "monthly_commitments": ("number", ("monthlyCommitments", "existingCommitments", "commitments", "monthlyObligations")),
    "income_verified": ("boolean", ("incomeVerified", "isIncomeVerified")),
    "bureau.status": ("string", ("bureauStatus", "creditBureau.status", "creditBureauStatus")),
    "bureau.score": ("number", ("bureauScore", "creditScore", "creditBureau.score")),
    "fraud.status": ("string", ("fraudStatus", "fraudAlertStatus", "alertStatus", "fraudCheck.status", "fraudFlag", "suspicious")),
    "fraud.alert_resolved": ("boolean", ("fraudAlertResolved", "alertResolved", "fraud.alertResolved")),
    "identity.status": ("string", ("identityStatus", "idvStatus", "kycStatus", "ekyc.status", "kyc.status")),
}

OUTPUT_FIELDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "case_id": ("string", INPUT_FIELDS["case_id"][1]),
    "decision": ("string", ("finalStatus", "outcome", "decisionStatus", "status", "result", "verdict", "finalDecision")),
    "pd": ("probability", ("probabilityOfDefault", "probability_of_default", "defaultProbability", "riskScore")),
    "model_version": ("string", ("modelVersion", "modelVer", "scorecardVersion")),
    "policy_version": ("string", ("policyVersion", "rulesVersion", "ruleSetVersion", "strategyVersion")),
    "reason_codes": ("list", ("reasonCodes", "reasons", "declineReasons", "reason_list")),
    "trace_id": ("string", ("traceId", "requestId", "correlationId", "x-trace-id")),
}

DECISION_VALUES: dict[str, str] = {}
for canon, words in {
    "APPROVE": ("APPROVE", "APPROVED", "ACCEPT", "ACCEPTED", "AUTO_APPROVE", "AUTO_APPROVED", "PASS"),
    "DECLINE": ("DECLINE", "DECLINED", "REJECT", "REJECTED", "AUTO_DECLINE", "DENY", "DENIED"),
    "REVIEW": ("REVIEW", "MANUAL_REVIEW", "PENDING_REVIEW", "MANUAL", "UNDER_REVIEW"),
    "REFER": ("REFER", "REFERRED", "REFERRAL", "ESCALATE", "ESCALATED"),
    "HOLD": ("HOLD", "ON_HOLD", "HELD", "SUSPEND", "SUSPENDED"),
}.items():
    for w in words:
        DECISION_VALUES[re.sub(r"[^A-Z0-9]", "", w.upper())] = canon


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _leaves(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict) and v:
                out.update(_leaves(v, path))
            else:
                out[path] = v
    return out


def _type_ok(expected: str, v: Any) -> bool:
    if v is None:
        return True  # cannot tell from a null sample
    if expected == "string":
        return isinstance(v, str)
    if expected == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if expected == "boolean":
        return isinstance(v, bool)
    if expected == "probability":
        return isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1
    if expected == "list":
        return isinstance(v, list)
    return True


def _describe(v: Any) -> str:
    return "null" if v is None else type(v).__name__


def _match(fields: dict[str, Any], table: dict[str, tuple[str, tuple[str, ...]]]):
    """Returns (matches: canon -> (customer_path, how, value), mismatched, unmatched_customer)."""
    index: dict[str, tuple[str, str]] = {}
    for canon, (_, syns) in table.items():
        for s in syns:
            index.setdefault(_norm(s), (canon, "synonym"))
    for canon in table:  # exact names win over synonyms
        index[_norm(canon)] = (canon, "exact")
    matches: dict[str, tuple[str, str, Any]] = {}
    mismatched: list[dict[str, Any]] = []
    unmatched: list[str] = []
    # exact matches first so a synonym never steals a field that has an exact counterpart
    for path in sorted(fields, key=lambda p: index.get(_norm(p), ("", "z"))[1]):
        hit = index.get(_norm(path))
        if hit is None:
            unmatched.append(path)
            continue
        canon, how = hit
        if canon in matches:
            unmatched.append(path)
            continue
        v = fields[path]
        if not _type_ok(table[canon][0], v):
            mismatched.append({"customer_field": path, "canonical_field": canon, "expected": table[canon][0],
                               "sample_type": _describe(v),
                               "note": "Not mapped: the value has a different shape. Add a translation in your API or map it by hand."})
            continue
        matches[canon] = (path, how, v)
    return matches, mismatched, sorted(unmatched)


def suggest_mapping(sample_request: dict[str, Any] | None, sample_responses: list[dict[str, Any]]) -> dict[str, Any]:
    if not sample_responses:
        raise MappingError("at least one sample response is required")
    if not all(isinstance(r, dict) for r in sample_responses):
        raise MappingError("each sample response must be a JSON object")
    notes: list[str] = []
    fields_out: list[dict[str, Any]] = []

    # ---- request ------------------------------------------------------------------------
    req_map: dict[str, str] = {}
    req_mismatched: list[dict[str, Any]] = []
    req_unmatched: list[str] = []
    request_identity = False
    if sample_request is not None:
        if not isinstance(sample_request, dict):
            raise MappingError("the sample request must be a JSON object")
        m, req_mismatched, req_unmatched = _match(_leaves(sample_request), INPUT_FIELDS)
        request_identity = bool(m) and all(how == "exact" and path == canon for canon, (path, how, _) in m.items())
        for canon, (path, how, v) in sorted(m.items()):
            req_map[path] = canon
            fields_out.append({"direction": "request", "canonical_field": canon, "customer_field": path, "matched_by": how})

    # ---- response -----------------------------------------------------------------------
    merged: dict[str, Any] = {}
    for r in sample_responses:
        for k, v in _leaves(r).items():
            merged.setdefault(k, v)
    resp_m, resp_mismatched, resp_unmatched = _match(merged, OUTPUT_FIELDS)
    resp_map: dict[str, dict[str, Any]] = {}
    decision_values: dict[str, str] = {}
    unknown_values: list[str] = []
    for canon, (path, how, _) in sorted(resp_m.items()):
        spec: dict[str, Any] = {"source": path}
        if canon == "decision":
            seen = []
            for r in sample_responses:
                v = _leaves(r).get(path)
                if isinstance(v, str) and v not in seen:
                    seen.append(v)
            for v in seen:
                c = DECISION_VALUES.get(re.sub(r"[^A-Z0-9]", "", v.upper()))
                if c:
                    decision_values[v] = c
                else:
                    unknown_values.append(v)
            if decision_values and any(k != v for k, v in decision_values.items()) or unknown_values:
                spec["values"] = decision_values
        if canon == "pd" and path.lower() != "pd" and "probab" not in path.lower():
            notes.append(f"'{path}' was matched to pd because its sample values are between 0 and 1. Confirm it is a probability of default, not a score.")
        resp_map[canon] = spec
        fields_out.append({"direction": "response", "canonical_field": canon, "customer_field": path, "matched_by": how})

    if "decision" not in resp_map:
        notes.append("No decision field was recognised. Map it by hand; a run cannot judge responses without it.")
    if unknown_values:
        notes.append(f"Decision values {unknown_values} were not recognised. Map each to one of {list(DECISIONS)} before saving.")
    unseen = sorted(set(DECISIONS) - set(decision_values.values()))
    if "values" in resp_map.get("decision", {}) and unseen:
        notes.append(f"No sample showed {unseen}. Add those values when you know your system's wording; "
                     "an unmapped value during a run is recorded as an ERROR, never a pass.")

    response_identity = all(spec == {"source": canon} for canon, spec in resp_map.items())
    mapping: dict[str, Any] = {}
    if req_map and not request_identity:
        mapping["request"] = {"fields": req_map}
    if resp_map and not response_identity:
        mapping["response"] = {"fields": resp_map}
    if sample_request is not None and request_identity and req_unmatched == []:
        notes.append("The request already uses ModelAssure's field names, so no request mapping is needed.")
    if response_identity and resp_map:
        notes.append("The response already uses ModelAssure's field names, so no response mapping is needed.")

    # ---- round-trip check ---------------------------------------------------------------
    checks = []
    try:
        validate_mapping(mapping)
        valid = True
    except MappingError as e:
        valid = False
        notes.append(f"The draft mapping is not yet valid: {e}")
    if valid:
        for i, r in enumerate(sample_responses):
            out, errs, _ = normalize_response(mapping, r)
            checks.append({"sample": i, "ok": not errs, "normalized": out, "errors": errs})

    missing_inputs = sorted(set(INPUT_FIELDS) - set(req_map.values())) if sample_request is not None else []
    return {
        "mapping": mapping,
        "valid": valid,
        "fields": fields_out,
        "unmatched": {"request": req_unmatched, "response": resp_unmatched},
        "type_mismatches": req_mismatched + resp_mismatched,
        "canonical_inputs_not_found": missing_inputs,
        "decision_values": decision_values,
        "response_checks": checks,
        "notes": notes,
    }
