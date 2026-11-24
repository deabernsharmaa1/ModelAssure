# Decisions and deviations from the specification

The build follows *ModelAssure — Complete Product & Technical Specification v1.0 (24 Nov 2026)*.
Where the specification left a gap that would have caused defects, the change below was agreed
before the build started. Each item names what changed, why, and where it lives.

## 1. Approved changes have a data model

**Gap.** QA-04 and demo scenario 3 require telling an approved policy change apart from a
violation, but a run referenced a single policy set.

**Decision.** A run references the **candidate** policy set (what the new build must satisfy) and
the **baseline** policy set (what was in force for the old build). When a decision changes:

1. a `PROHIBIT` change-control rule makes it `FAIL`;
2. a matching *approved change declaration* (`expected_changes`) explains it;
3. otherwise, if the baseline's decision would violate a candidate policy that the candidate
   satisfies, the change is explained by that approved revision;
4. anything unexplained is `CHANGED / UNEXPECTED` and flagged for human review.

A declared change **never excuses a violation**: a changed decision that also breaks a policy is
`FAIL` with change class `VIOLATION` (found during the build; see `test_declared_change_never_excuses_a_violation`).

Code: `modules/evaluation/engine.py::_classify_change`; API `POST /v1/runs` (`baseline_policy_set_id`).

## 2. Policy conditions see the scenario, not only the request

Injected dependency faults (e.g. a bureau timeout) are not in the request body, so a condition
can reference `scenario.faults.bureau`. Namespaces: `input`, `output`, `baseline`, `scenario`,
`derived`, `params`. Code: `modules/policies/dsl.py`.

## 3. Derived fields and named parameters (POL-003)

The spec's DSL could not express debt-service coverage (a ratio) or a customer-supplied threshold.
Added declarative `derived_fields` (`add/sub/mul/div/min/max/abs/neg/coalesce/round`, no code
execution) and `value_ref: "params.min_dscr"`. All thresholds come from the institution.

## 4. Change-control rules are their own rule type (POL-006)

"Candidate version changes an important decision" compares two versions rather than asserting on
the candidate's output, so it is modelled as `change_controls` with action `FLAG` or `PROHIBIT`.

## 5. Result-state precedence and three-valued logic

`SKIPPED > ERROR > FAIL > INCONCLUSIVE > CHANGED > PASS`, documented in
`modules/evaluation/engine.py`. Conditions use Kleene logic: a missing field makes a condition
UNKNOWN, which yields `INCONCLUSIVE`, never `PASS`. Per-leaf `if_missing` and per-policy
`missing_data: not_applicable` make the author's intent explicit. When two applicable policies
demand disjoint decisions, the higher `precedence` wins; equal precedence is rejected at
validation time (FR-13) and is `INCONCLUSIVE` at run time.

## 6. Reproducibility: generate once, replay forever

Suites are generated **once** (fixed seed) when a suite version is created, stored and hashed.
Runs replay stored cases. Volatile response fields (`trace_id`) are excluded from comparisons and
decision hashes.

Generation is also reproducible on its own: the same config, seed and `GENERATOR_VERSION` give the
same suite on any machine. The first build drew regression cases through Hypothesis's test
runner, but its engine returned different examples for the same seed depending on the run
context (for example inside pytest versus a plain process). Audit evidence cannot depend on
that, so the property generator now uses a seeded, edge-biased sampler (`Draw`) that, like
Hypothesis, over-samples range ends. Hypothesis is still used where it fits: property tests of
ModelAssure's own policy engine. Each stored suite records the `generator_version` it was built with.

## 7. Fault injection against real customers

Fault injection works because the fictional bank calls a dependency simulator that ModelAssure
controls. A real customer system calls its own bureau, so for pilots either the customer's
sandbox is pointed at ModelAssure's dependency simulator (`workers/simulators/dependency_mock`, a
standalone service), or dependency results are passed in the request (as in spec §7.1). Cases that
need fault injection without a configured simulator become `INCONCLUSIVE`, not `PASS`.

## 8. Schema additions

`test_runs` gained `project_id`, `baseline_policy_set_id`, `config_snapshot` + `snapshot_hash`,
`scope`, `parent_run_id`; executions store baseline and candidate outcomes separately with
evidence in encrypted object storage; findings link to many executions; added `jobs`,
`idempotency_keys`, `datasets`. All tenant tables carry `organization_id` for row-level security.

## 9. Simplifications

- **Queue:** PostgreSQL (`FOR UPDATE SKIP LOCKED` + leases) instead of Celery + Redis. One fewer
  service; each execution is committed individually, so a crashed worker loses nothing (NFR-04).
- **Login:** seeded development users behind an OIDC-ready interface. In `oidc` mode tokens are
  verified against the IdP's JWKS and MFA is enforced from the token's `amr` claim for privileged
  actions. The OIDC path is implemented but was not exercised against a real IdP in this build.

## 10. Additional safeguards found during the build

- **A fix verified on another build does not make the original build releasable.** If a finding
  from a 3.2.0 run is resolved by a retest on 3.2.1, the 3.2.0 run's assessment stays FAIL and
  says so. Otherwise a reviewer could approve the defective build.
- **A partial retest is not a release basis.** Retest runs are always assessed INCONCLUSIVE; a
  full run of the fixed build is required.
- **Findings group by root cause** (the primary violated policy by precedence), listing every
  co-violated rule, so one defect produces one finding.
- **Wrong deployment detection (FR-10).** If an endpoint reports a model version different from
  the registered version, the case is `ERROR / VERSION_MISMATCH`.
- **Retention.** `scripts/purge_evidence.py` deletes raw request/response payloads after the
  institution's retention period while keeping outcomes and hashes; the purge is audit-logged.

## 11. Additions after market research

Market research showed that the buyers' pain is passing supervisory scrutiny and proving that
vendor updates were tested, more than running tests. Four additions follow from that.

- **Regulatory mapping** (`modules/compliance/`). Each control in BNM's AI feedback statement,
  RMiT and MAS's AI risk guidelines is checked against the run's own evidence (MET / PARTIAL /
  NOT_MET / NO_EVIDENCE). Controls only partly provable by testing are capped at PARTIAL. It is
  labelled as not a legal or compliance opinion. The AICB AI MRM framework has a slot for when it
  is published; nothing is claimed for it now.
- **Faster onboarding.** Policy templates ship with every threshold empty (`PARAMETER_NOT_SET`
  blocks submission), preserving "ModelAssure never invents thresholds". The mapping helper
  maps only what it can see in samples, flags fields whose value shape differs, and never maps an
  unseen decision value (an unmapped value at run time is an ERROR, never a pass). The consumer
  credit profile has no simulator behind it; it is tested at unit and API level.
- **Signed evidence packs** (`modules/evidence/`). A zip with a manifest of SHA-256 hashes signed
  with a per-organisation Ed25519 key, the public key, and a standalone verifier that needs only
  `cryptography`. Design choices:
  - The private key sits in the secret store; the table keeps a reference, and a trigger makes key
    material immutable. Rotation retires the old key, which stays listed, so earlier packs remain
    attributable.
  - Recipients must check the key fingerprint against one obtained separately: a pack re-signed
    with another key is internally consistent, and the verifier says so only if you pass
    `--expect-fingerprint` (tested).
  - The pack says plainly what it proves (origin and integrity) and what it does not (that the
    tests were adequate or ran against the recipient's system).
  - The run configuration is redacted (secret references removed, endpoints replaced by hashes).
    Case-level raw evidence is excluded by default because it can contain applicant data.
  - Packs leave the organisation, so they are issued by Risk Managers, Reviewers, Auditors and
    Admins, not by the Engineers and Analysts who build and test the system.

- **Release gate for CI/CD** (`modules/gate/`, [release-gate.md](release-gate.md)). Design choices:
  - A pipeline key has its own service identity with a `CI` role that holds only the gate
    permissions, is scoped to one project, expires, and stores only a SHA-256 of its secret.
  - Authentication must find a key before the organisation is known. A `SECURITY DEFINER`
    function returns the one row for a key id, so the table keeps row-level security for the
    application role. Key identity is immutable and keys are revoked, never deleted.
  - Segregation of duties follows the key to the admin who created it.
  - The gate reuses the setup of the project's latest completed full run, and verifies the new
    endpoint before running. It never changes the project's own candidate selection.
  - A PASS never approves a release. Exit codes: 0 PASS, 1 FAIL, 2 INCONCLUSIVE, 3 error.

## Not in Version 1 (per spec §1.4 and §16)

Model training, credit-bureau integrations, real-time fraud prevention, insurance, AI-agent
governance, Web3; customer-hosted runner, Python model packages, vendor adapters; calibration,
fairness and drift (Phase 2); GenAI prompt attacks (Phase 3); production monitoring (later). CI/CD pipeline gating (BR-10) was
brought forward: see section 11 and [release-gate.md](release-gate.md).
