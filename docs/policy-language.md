# Policy language

Policies are **data**, validated against a closed grammar. Nothing a user writes is ever executed
as code. The institution supplies every rule and every threshold; ModelAssure never invents credit
eligibility or regulatory thresholds.

A policy set has this shape:

```json
{
  "label": "2026-11",
  "parameters": { "min_dscr": 1.5 },
  "derived_fields": [
    { "name": "dscr", "operator": "div", "args": ["monthly_cash_available", "monthly_debt_service"] }
  ],
  "policies": [ … ],
  "change_controls": [ … ],
  "expected_changes": [ … ],
  "severity_rules": { "UNEXPECTED_CHANGE": "MEDIUM", "TECHNICAL_ERROR": "HIGH" },
  "release_criteria": { "fail_on_unresolved_severities": ["CRITICAL", "HIGH"], "max_error_rate": 0.0 }
}
```

See `decision_simulator/fixtures/policy_set_2026-11.json` for a complete example.

### Templates

`GET /v1/policy-templates` lists ready-made starting points (SME lending core controls; consumer
credit / BNPL affordability). Every threshold in a template is `null`. A parameter that is still
`null` is a validation error (`PARAMETER_NOT_SET`), so a policy set cannot be submitted for
approval until the institution has entered its own values. `POST /v1/policy-templates/{id}/instantiate`
returns the filled document and its validation without saving anything; the document records
`source_template` (id and hash) for provenance, which never affects evaluation.

## Policies (assertions on the candidate's decision)

```json
{
  "policy_id": "POL-001",
  "version": 1,
  "description": "Mandatory bureau data unavailable requires manual review",
  "when":   { "field": "bureau.status", "operator": "eq", "value": "UNAVAILABLE", "if_missing": "false" },
  "assert": { "field": "decision", "operator": "eq", "value": "REVIEW" },
  "severity": "CRITICAL",
  "precedence": 80,
  "missing_data": "inconclusive",
  "enabled": true
}
```

- `when` is optional (absent = always applies). `assert` is required.
- Normalised decisions: `APPROVE`, `DECLINE`, `REVIEW`, `HOLD`, `REFER`. Map the vendor's own
  values to these in the connection's field mapping.

## Conditions

Leaf: `{"field": …, "operator": …, "value": …}`, or `"value_ref": "params.<name>"` instead of `value`.

| Operator | Meaning |
|---|---|
| `eq`, `ne` | equal / not equal |
| `gt`, `gte`, `lt`, `lte` | numeric comparison (both sides must be numbers) |
| `in`, `not_in` | membership in a non-empty list |
| `contains` | list contains a value, or string contains a substring |
| `exists`, `not_exists` | field present and not null / absent or null |

Groups: `{"all": [...]}`, `{"any": [...]}`, `{"not": {...}}`, or the operator form
`{"operator": "all", "conditions": [...]}`.

### Field namespaces

| Prefix | Contents | Default for |
|---|---|---|
| `input.` | canonical decision request | `when` |
| `output.` | normalised candidate response | `assert` |
| `baseline.` | normalised baseline response | |
| `scenario.` | scenario metadata, e.g. `scenario.faults.bureau` = `TIMEOUT` | |
| `derived.` | declared derived fields | |
| `params.` | institution parameters | |

### Missing data: three-valued logic

A condition on a missing field is **UNKNOWN**, not false. If a policy cannot tell whether it
applies, the case is `INCONCLUSIVE` and never silently passes. To state intent explicitly:

- per leaf: `"if_missing": "false"` (or `"true"`), e.g. "bureau status is UNAVAILABLE; if the
  bureau block is absent, this leaf is false";
- per policy: `"missing_data": "not_applicable"` when another policy covers missing data
  (POL-003 in the demo relies on POL-007 for missing financials).

`all` is false if any child is false; `any` is true if any child is true; otherwise UNKNOWN
propagates.

## Derived fields

`add`, `sub`, `mul`, `div`, `min`, `max`, `abs`, `neg`, `round`, `coalesce`. Arguments are numbers
or field references to `input`, `scenario`, `params` or **earlier** derived fields. Division by
zero yields a missing value (so the dependent condition is UNKNOWN).

## Precedence and conflicts

If two applicable policies accept disjoint decisions (e.g. fraud → `HOLD`, bureau → `REVIEW`),
the higher `precedence` wins and the other is recorded as `SUPERSEDED` for that case. At
validation time, ModelAssure reports:

- `POLICY_CONFLICT` (error): disjoint decisions, conditions that can overlap, **equal**
  precedence. The set cannot be submitted until this is fixed.
- `POLICY_SUPERSEDED` (warning): which policy wins when both apply.

Overlap is disproved only when the conditions are provably exclusive (same field with
incompatible values or ranges); otherwise ModelAssure assumes they can overlap.

## Change controls (cross-version rules)

```json
{ "rule_id": "CHG-002", "description": "A hold must never become an automatic approval",
  "from": ["HOLD"], "to": ["APPROVE"], "action": "PROHIBIT", "severity": "CRITICAL" }
```

`FLAG` puts the change in front of a reviewer; `PROHIBIT` fails the case. An optional `when`
restricts the rule.

## Approved changes

```json
{ "change_id": "EXP-2026-11-01", "reference": "CR-2026-114",
  "description": "Credit Committee raised the minimum DSCR from 1.25 to 1.50",
  "when": { "all": [ { "field": "derived.dscr", "operator": "gte", "value": 1.25 },
                     { "field": "derived.dscr", "operator": "lt",  "value": 1.5 } ] },
  "from": ["APPROVE", "REVIEW"], "to": ["REFER"] }
```

A decision change matching a declaration is `CHANGED / EXPECTED`. Changes are also recognised as
expected when the baseline's decision would violate a revised candidate policy that the candidate
satisfies. A declaration **never** excuses a policy violation.

## Severity and release criteria

Policy severities come from the institution. `severity_rules` sets the severity of finding types
that are not tied to a policy (`TECHNICAL_ERROR`, `UNEXPECTED_CHANGE`, `INCONCLUSIVE`,
`INVALID_INPUT_ACCEPTED`, `UNSAFE_ERROR_HANDLING`, `INCONSISTENT_DUPLICATE`, `EXPECTATION_MISMATCH`,
`VERSION_MISMATCH`). `release_criteria`:

| Key | Default | Meaning |
|---|---|---|
| `fail_on_unresolved_severities` | `["CRITICAL"]` | unresolved failing findings at these severities → assessment FAIL |
| `mandatory_categories` | all categories in the suite | must be executed and not skipped |
| `max_error_rate` | `0.0` | above this → INCONCLUSIVE |
| `max_inconclusive_rate` | `0.0` | above this → INCONCLUSIVE |

## Lifecycle

Draft → (validate) → Pending review → Approved / Rejected. The author or submitter cannot approve.
Approved sets are immutable (enforced by a database trigger); a change is a new version with a
recorded parent, and the builder shows the diff, including behaviour changed via parameters.
