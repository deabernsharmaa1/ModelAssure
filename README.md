# ModelAssure ReleaseGate

**Independent release assurance for financial decision systems.** ModelAssure tests whether a
proposed change to a lending decision system (credit model, fraud engine, bureau integration,
business rules) introduces policy violations or operational risk *before* it is deployed. It
compares the current and proposed versions, injects controlled failures, evaluates every decision
against the institution's approved policies, and produces reproducible evidence for engineers,
risk managers, auditors and release approvers.

> Product principle: test the complete financial decision, not just the accuracy of one model.
>
> ModelAssure supports human release decisions. It is not a regulator, it does not certify
> compliance, and it never authorises or performs a production deployment.

Version 1 scope: **SME lending decision systems**, tested through an authorised sandbox REST API
or approved recorded outputs, using synthetic data by default.

---

## What it does

| Step | Who | What happens |
|---|---|---|
| 1. Create a project | Risk Manager | Describe the release: system, baseline and candidate versions, risk class, sandbox |
| 2. Register versions & connect | Engineer | Immutable version registry; sandbox connections with write-only credentials, field mapping, verification |
| 3. Approve policies | Risk Manager + Reviewer | Structured, versioned rules; four-eyes approval; conflicts detected before execution |
| 4. Build scenarios | Analyst | Deterministic suite: normal, boundary, missing data, dependency faults, credit/fraud conflicts, duplicates, invalid input, property-based regression, historical replay |
| 5. Execute | Worker | Baseline and candidate called for every case; responses normalised and validated; policies applied; versions compared |
| 6. Investigate | Analyst / Engineer | Failures grouped into findings by root cause, with reproducible evidence and an evidence-backed suspected cause |
| 7. Fix & retest | Engineer | Retest affected cases on the fixed build; original evidence kept unchanged |
| 8. Review & report | Reviewer | Release assessment (PASS / FAIL / INCONCLUSIVE), human decision with segregation of duties, versioned PDF/JSON reports |

Result states: `PASS`, `FAIL`, `CHANGED` (classified `EXPECTED` or `UNEXPECTED`), `ERROR`,
`INCONCLUSIVE`, `SKIPPED`. A technical failure is never counted as a pass, and a different
decision is not automatically a wrong one: approved policy changes are recognised as expected.

## The demonstration

A fictional lender, *Tanjung Kredit Berhad*, runs build **3.1.0** in production and proposes
**3.2.0**. The candidate contains two seeded defects and one approved change:

1. **Bureau check fails → candidate approves anyway** (POL-001 / POL-005, critical).
2. **Unresolved high-risk fraud alert overridden** (POL-002 and prohibited HOLD→APPROVE, critical).
3. **Minimum DSCR raised from 1.25 to 1.50** by the Credit Committee: decisions change, and
   ModelAssure classifies them as *expected*, not defects.

It also surfaces a side effect nobody declared: some previously declined high-risk applicants
are now only referred. That is flagged for human review. Build **3.2.1** fixes the defects;
retest, full run, assessment PASS and reviewer approval complete the story.

Run the whole thing in one command (needs PostgreSQL, see below):

```bash
python scripts/demo.py --reset      # prints the story, writes PDF/JSON reports to var/demo/
```

## Run it

### Option A: Docker Compose

```bash
docker compose up -d --build
docker compose run --rm migrate python scripts/load_demo.py --api http://api:8000 \
  --baseline http://simulator-baseline:8101 --candidate http://simulator-candidate:8102 \
  --dependency http://dependency-simulator:8200
open http://localhost:3000
```

Services: `postgres`, `migrate` (schema + demo users), `api` (:8000, OpenAPI at `/docs`),
`worker`, `web` (:3000), and the fictional bank (`simulator-baseline` :8101,
`simulator-candidate` :8102, `dependency-simulator` :8200).

> The Compose file and Dockerfiles were written and syntax-checked (`docker compose config`) but
> the images could not be built in the environment this was developed in (no access to Docker
> Hub). Every container command is exercised by the local-process setup below.

### Option B: local processes

Requirements: Python 3.11+, Node 20+, PostgreSQL 14+.

```bash
pip install -r requirements-dev.txt
(cd apps/web && npm ci)

# PostgreSQL: an owner role for migrations and a separate NON-owner role for the application,
# so row-level security always applies to the app.
psql -c "CREATE ROLE modelassure LOGIN PASSWORD 'modelassure' SUPERUSER;"   # or a plain owner role
psql -c "CREATE ROLE modelassure_app LOGIN PASSWORD 'modelassure_app';"
createdb -O modelassure modelassure
createdb -O modelassure modelassure_test     # for the test suite

./scripts/dev_up.sh          # migrations, demo users, simulators, API, worker, web
python scripts/load_demo.py  # fictional bank + a completed run of the defective candidate
./scripts/dev_down.sh
```

### Demo users (development mode only)

Password for all: `ModelAssure!2026`

| User | Role |
|---|---|
| risk@tanjung.example | Risk Manager |
| engineer@tanjung.example | Engineer |
| analyst@tanjung.example | Model Validation Analyst |
| reviewer@tanjung.example | Release Reviewer |
| auditor@tanjung.example | Auditor |
| admin@tanjung.example | Admin |
| lead@tanjung.example | Engineer **and** Reviewer (shows that a builder cannot approve their own release) |
| admin@otherlender.example | Admin of a second organisation (tenant isolation) |

## Tests

```bash
pytest                                # 191 tests: unit, contract, integration, security, end-to-end
python scripts/ui_smoke.py            # browser workflow (needs the running stack, Chrome, Playwright)
(cd apps/web && npm run build)        # type-checks and builds the frontend
```

Integration tests use a real PostgreSQL database (`modelassure_test`) and start the fictional
bank as real HTTP processes. See [docs/spec-traceability.md](docs/spec-traceability.md) for
which test proves which acceptance criterion (QA-01 – QA-12, NFRs, Definition of Done).

## Repository layout

```
apps/api/            FastAPI modular monolith (identity, projects, connectors, policies,
                     scenarios, runner, evaluation, findings, reports, audit)
apps/web/            Next.js + TypeScript + Tailwind frontend (seven principal screens + audit)
workers/executor/    queue worker that executes runs
workers/simulators/  dependency simulator (bureau / fraud / identity fault injection)
workers/report_generator/  offline report verification CLI
decision_simulator/  the fictional bank: baseline 3.1.0, candidate 3.2.0 / 3.2.1, fixtures
test_suites/         suite definitions (lending, fraud, dependency failures)
infrastructure/      Alembic migrations (schema, RLS, immutability) and Docker files
scripts/             seed, demo, load_demo, dev_up/down, ui_smoke, purge_evidence
docs/                architecture, policy language, decisions, spec traceability
```

## Documentation

- [Architecture](docs/architecture.md): components, data flow, security model
- [Policy language](docs/policy-language.md): how to write policies, change controls and approved changes
- [Decisions and deviations from the spec](docs/decisions.md): what was changed and why
- [Spec traceability](docs/spec-traceability.md): requirement → implementation → test

## Production readiness

This is a working Version 1 MVP against a fictional bank, not a bank-certified platform. Before a
real customer pilot: connect an OIDC identity provider with MFA (`MA_AUTH_MODE=oidc`,
`MA_REQUIRE_MFA_FOR_PRIVILEGED=true`), use S3 with server-side encryption and a managed key,
replace every default secret, put the web app behind a backend-for-frontend so tokens live in
httpOnly cookies, add API rate limiting, commission a penetration test, and document data-processing
roles, retention and cross-border arrangements with the institution (spec §12.3).
