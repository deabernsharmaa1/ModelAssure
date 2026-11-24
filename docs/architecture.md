# Architecture

A modular monolith with isolated background workers (spec §5).

```
 Browser ── Next.js web (apps/web) ──HTTPS/JSON──► FastAPI API (apps/api)
                                                     │  identity · projects · connectors · policies
                                                     │  scenarios · runner · evaluation · findings
                                                     │  reports · audit
                                                     ▼
                                  PostgreSQL (system of record, row-level security, job queue)
                                                     ▲
                       Worker(s) (workers/executor) ─┘  claim jobs (SKIP LOCKED), execute runs
                                │
                                │  sandbox only, allow-listed, rate-limited, traceparent
                                ▼
           Authorised customer test environment:  baseline API · candidate API
                                                   dependency simulator (fault injection)

 Encrypted object storage (local Fernet or S3 SSE): evidence per case, reports, datasets
```

## Request path of a run

1. `POST /v1/runs` validates the configuration and **locks** it into `config_snapshot`: suite
   hash, version checksums, both policy sets with their content and approval status, and
   connection settings (secret *references* only). The snapshot hash is stored; a database trigger
   prevents any change to it. A job is queued in the same transaction.
2. A worker claims the job and leases it. Before calling anything it re-checks every endpoint
   against the egress policy (DNS re-resolution), resolves credentials from the secret store, and
   registers injected faults with the dependency simulator.
3. For each stored case, in a bounded thread pool (per-connection concurrency, token-bucket rate
   limit, per-request timeout, no redirects): call baseline, call candidate (repeat for duplicate
   tests), normalise and validate both responses, evaluate the candidate against the approved
   candidate policies and the baseline against its own, classify any decision change, and assign
   the result state.
4. Each execution is written with its evidence (input, raw requests and responses,
   transformations, evaluation, trace id, schema versions) to object storage, hashed, and
   committed individually. A worker crash loses nothing; the next worker resumes.
5. On completion: regression metrics, findings grouped by root cause, release assessment, audit event.

## Security model

| Control | Implementation |
|---|---|
| Tenant isolation | Every tenant table has `organization_id` and a PostgreSQL RLS policy on `app.current_org`, set per transaction. The API and worker connect as a non-owner role, so RLS cannot be bypassed by application bugs. Tested directly against the database. |
| Least privilege | Six roles; permission matrix in `modules/identity/auth.py`. Denied requests are audit-logged in their own transaction. |
| Segregation of duties | Policy authors/submitters cannot approve their own policy set. Whoever registered the candidate version, configured its connection or started the run cannot review the release. |
| MFA | With an OIDC IdP, privileged actions require `mfa` in the token's `amr` claim (`MA_REQUIRE_MFA_FOR_PRIVILEGED`). |
| Credentials | Write-only; stored encrypted in the secret store (`local:`) or read from platform-injected environment secrets (`env:`). Tables hold references only. |
| Egress | Only hosts on the platform allowlist (and the organisation's approved destinations); http(s) only; no URL credentials; metadata, link-local, multicast and unspecified addresses always blocked; https required in production; redirects never followed; `SANDBOX` environment only. ModelAssure never discovers endpoints. |
| No code execution | Policies and mappings are validated data. Customer code is never run (packaged-model execution is a later, isolated-worker feature). |
| Evidence integrity | Executions, suites, cases, versions, reviews and artifacts are append-only (triggers). Evidence and reports are SHA-256 hashed and re-verified on read. Reports carry a results digest that is stable across regeneration. |
| Audit | Hash-chained per organisation, append-only (trigger + `REVOKE UPDATE, DELETE`), verifiable via `GET /v1/audit-events/verify`. Tampering by a DBA is detected (tested). |
| Data protection | Encryption at rest for objects and secrets; request bodies are never logged; CSV exports exclude inputs; raw evidence retention purge. |

## Technology

Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2, Alembic, PostgreSQL 16, HTTPX, Hypothesis,
pandas, pandera, WeasyPrint (PDF), OpenTelemetry (W3C trace context), Next.js 16, React 19,
TypeScript, Tailwind 4, Radix UI primitives. No Kubernetes, Kafka, graph database or agent
framework (spec §5.1).

## Configuration

All settings are environment variables with the `MA_` prefix (`apps/api/settings.py`). See
`.env.example`.
