# Specification traceability

Requirement → where it is implemented → how it is verified. Tests are under `tests/`; paths to
code are relative to `apps/api/` unless stated.

## Acceptance tests (spec §14)

| ID | Scenario | Verified by |
|---|---|---|
| QA-01 | Candidate approves when mandatory bureau data is absent → violation | `unit/test_engine.py::test_qa01_*`, `integration/test_runs.py::test_qa01_bureau_failure_approval_detected` (33 cases incl. live bureau faults), `end_to_end/…::test_findings_grouped_with_evidence_and_suspected_cause` |
| QA-02 | Candidate overrides unresolved high-risk fraud alert → violation | `unit/test_engine.py::test_qa02_*`, `integration/test_runs.py::test_qa02_fraud_override_detected` |
| QA-03 | Candidate follows all approved policies → passed | `integration/test_runs.py::test_qa03_fixed_candidate_passes_all_policies` (3.2.1: 0 FAIL, 0 ERROR) |
| QA-04 | Approved revised policy changes a decision → change recorded, no violation | `unit/test_engine.py::test_qa04_*`, `integration/test_runs.py::test_qa04_*`, `end_to_end/…::test_expected_change_creates_no_finding` |
| QA-05 | Candidate API times out → error/inconclusive, never passed | `unit/test_engine.py::test_qa05_qa06_*`, `integration/test_runs.py::test_qa05_qa06_technical_failures_never_pass[QA05-TIMEOUT]` |
| QA-06 | Candidate returns malformed JSON → schema error captured | same, `[QA06-MALFORMED]`, plus HTTP 500 and unmapped decision values |
| QA-07 | Same case rerun with unchanged versions → identical result | `integration/test_runs.py::test_qa07_rerun_reproduces_identical_results` (full suite, per-case comparison) |
| QA-08 | Policy changes after a run → historical run unchanged | `integration/test_runs.py::test_qa08_*` (snapshot hash re-verified; summary unchanged) |
| QA-09 | Organisation A user requests Organisation B's report → denied | `security/test_tenancy_and_access.py::test_other_org_cannot_read_project`, `::test_rls_enforced_in_database_even_without_app_checks` |
| QA-10 | Critical finding corrected → retest passes, history retained | `end_to_end/…::test_qa10_retest_passes_and_resolves_findings`, `::test_qa10_historical_evidence_retained` |
| QA-11 | Report regenerated → correct version, original results preserved | `end_to_end/…::test_qa11_reports_versioned_and_reproducible` (v1 and v2 share the results digest) |
| QA-12 | Unapproved user attempts sign-off → denied and logged | `end_to_end/…::test_qa12_unauthorised_signoff_denied_and_logged`, `::test_builder_cannot_approve_own_release` |

## Non-functional requirements (spec §12.1)

| ID | Target | Result / verification |
|---|---|---|
| NFR-01 | p95 page response < 2 s | 0.84 s p95 over 45 loads of the nine screens (`scripts/measure_ui.py`, headless Chrome, local stack; includes Playwright's 0.5 s network-idle wait) |
| NFR-02 | ≥ 1,000 synthetic cases in < 10 min | 1,085 cases in ≈ 8 s against the local simulator (`integration/test_runs.py::test_nfr02_*`, `scripts/demo.py`) |
| NFR-03 | Per-connection concurrency, default 5 | `connections.max_concurrency` (default `MA_DEFAULT_CONNECTION_CONCURRENCY=5`, max 20), semaphore + token-bucket rate limit in `modules/connectors/client.py` |
| NFR-04 | Failed workers cannot lose results | `integration/test_runs.py::test_worker_crash_recovery` (crash after 59 cases, resume to full count, no duplicates) |
| NFR-05 | Deterministic repeatability | QA-07; suites generated once and hashed (`unit/test_generators_and_simulator.py::test_generation_is_deterministic`) |
| NFR-06 | Timeouts and invalid responses explicit | QA-05/QA-06; `modules/connectors/client.py::_interpret` |
| NFR-07 | Evidence hashed and version-linked | `integration/test_runs.py::test_evidence_is_stored_and_integrity_checked` |
| NFR-08 | Reports preserve original configuration and findings | QA-11; report embeds snapshot hash and results digest |

## Functional requirements (spec §4)

| ID | Implementation |
|---|---|
| FR-01 Organisation workspace | `organizations` + RLS on every tenant table (`infrastructure/migrations/versions/0002_*`) |
| FR-02 Roles | `modules/identity/auth.py` (ADMIN, RISK_MANAGER, ENGINEER, ANALYST, REVIEWER, AUDITOR); `security/…::test_least_privilege` |
| FR-03 Project creation | `POST /v1/projects` with all required fields (§4 "Required project fields") |
| FR-04 Version registry | `POST /v1/systems/{id}/versions`; immutable (`integration/…::test_versions_are_immutable`) |
| FR-05 Audit history | `modules/audit/service.py`; hash chain; `security/…::test_tampering_detected_by_chain_verification` |
| FR-06 Register endpoints | `POST /v1/connections` |
| FR-07 Field mapping | `modules/connectors/mapping.py`; transformations kept in evidence |
| FR-08 Validate auth, schema, format | `POST /v1/connections/{id}/verify` (auth, auth enforced, mapping, schema, reported version) |
| FR-09 Timeouts, rate limits, concurrency | connection settings, locked into the run snapshot |
| FR-10 Record versions per response | `test_executions.observed_versions`; mismatch → `ERROR / VERSION_MISMATCH` |
| FR-11 Structured, versioned policies | `policy_sets` versions with parent links; `modules/policies/` |
| FR-12 Evaluate on normalised inputs and outputs | `modules/policies/policy_set.py::evaluate_policies` |
| FR-13 Conflicting/invalid policies detected before execution | `POLICY_CONFLICT`, `UNKNOWN_PARAMETER`, structural errors; `integration/…::test_invalid_and_conflicting_policies_flagged_before_execution` |
| FR-14 Review before activating a changed policy set | submit → approve by a different person; `::test_author_cannot_approve_own_policy_set` |
| FR-15 Policy IDs and explanations for failures | violation records carry policy id, version, expected, observed, reason; spec §7.4 rows (`unit/test_engine.py::test_qa01_*`) |

Module D (scenario categories): normal, boundary, missing data, dependency failure, cross-system
conflict, version regression, duplicates, invalid input and historical replay are all generated
(`modules/scenarios/generators.py`). Phase 2/3 categories are out of scope.

Module F (findings): ID, severity (suggested and institution-set), versions, related rules,
reproducing inputs, baseline/candidate outputs, expected vs actual, logs/timestamps (evidence and
trace id), evidence-backed suspected cause, owner, status, retest history; statuses Open,
Investigating, Fix Proposed, Ready for Retest, Resolved, Accepted Risk, Closed.

Module G (reports): all nine sections; PDF and JSON; CSV of permitted outcomes.

## Definition of Done (spec §18)

| Criterion | Evidence |
|---|---|
| Create a validation project | `POST /v1/projects`; *New project* screen |
| Register two versions of an authorised decision API | version registry + verified connections |
| Configure and approve executable policy rules | policy builder; four-eyes approval |
| Generate and execute controlled scenarios | suites + runs (1,085 cases) |
| Identify seeded violations with reproducible evidence | QA-01, QA-02; evidence viewer |
| Distinguish authorised changes from confirmed errors | QA-04; change class VIOLATION |
| Handle failed tests and API errors without false passes | QA-05, QA-06, baseline-error tests |
| Investigate, assign, resolve and retest findings | QA-10; `scripts/ui_smoke.py` drives it in the browser |
| Reviewer inspects evidence and records a decision | `::test_fixed_build_passes_with_flags_and_can_be_approved`; release screen |
| Export a complete versioned report | QA-11 |
| Tenant isolation and RBAC pass security tests | `tests/security/` |
| Works end-to-end against the fictional bank | `scripts/demo.py`, `scripts/ui_smoke.py`, `tests/end_to_end/` |

Commercial readiness (a prospective customer confirming the workflow solves a recurring problem)
is outside what software can demonstrate and remains open.
