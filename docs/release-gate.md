# Release gate for CI/CD pipelines

The release gate lets a build pipeline run the project's release suite against a new build and
stop the pipeline if the build breaks an approved policy. It covers BR-10 ("integrate with
software release pipelines"), which the specification scheduled for later.

A gate **PASS** means the defined tests passed under the stated configuration. It is not a
release authorisation. An authorised reviewer still records the release decision in ModelAssure,
and that reviewer cannot be the person who created the pipeline key.

## How it works

1. The pipeline deploys the new build to its sandbox.
2. It calls the gate with the new version and the sandbox endpoint.
3. ModelAssure registers the version, or reuses it if it already exists. Versions are immutable,
   so the same version number with a different expected model version is refused.
4. It copies the project's last verified candidate connection, keeping the mapping, credential
   reference, limits and dependency simulator but using the new endpoint, and verifies it. If
   verification fails, nothing runs and the gate returns an error.
5. It queues a full run with the same test suite and policy sets as the project's latest completed
   full run, against the project's baseline version.
6. The pipeline waits for the result and exits with its code.

Run the release suite once from ModelAssure first. The gate reuses that run's setup, so the
pipeline only supplies what changes: the version and endpoint.

## Exit codes

| Code | Meaning | Pipeline should |
|---|---|---|
| 0 | PASS: defined tests passed | continue to human release review |
| 1 | FAIL: release criteria violated | stop; see the blocking findings |
| 2 | INCONCLUSIVE: errors, inconclusive cases or incomplete evidence | stop; investigate |
| 3 | The gate could not run: bad key, endpoint failed verification, run failed, timeout | stop; fix the setup |

## Pipeline keys

An administrator creates a key under **Pipeline keys**. The key:

- is shown once; only its SHA-256 is stored;
- belongs to its own service identity with the `CI` role, which can only start and read gate runs;
- is scoped to one project, and expires within 1–365 days (default 90);
- makes its creator accountable: segregation of duties treats the creator as having started the
  runs, registered the versions and configured the connections that the key did;
- can be revoked at any time, after which it is refused immediately.

Every gate call is audit-logged under the key's identity, with the pipeline metadata (commit, ref,
pipeline URL) the CLI sends.

## Command-line client

`scripts/modelassure_gate.py` uses only the Python standard library. Copy it into the pipeline
repository or fetch it from this one.

```bash
MODELASSURE_API_KEY=mak_... python3 modelassure_gate.py \
  --api https://modelassure.example \
  --project <project id> \
  --version 3.2.1 \
  --endpoint https://sandbox.vendor.example/3.2.1/decisions \
  --expected-model-version 3.2.1      # optional; defaults to --version
```

Calls are idempotent per project, version and commit, so a retried pipeline step does not queue a
second run.

## GitHub Actions example

```yaml
name: release-gate
on:
  push:
    tags: ["v*"]

jobs:
  gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Deploy candidate to sandbox
        run: ./deploy-sandbox.sh "${GITHUB_REF_NAME#v}"        # your deployment
      - name: ModelAssure release gate
        env:
          MODELASSURE_API_KEY: ${{ secrets.MODELASSURE_API_KEY }}
        run: |
          python3 scripts/modelassure_gate.py \
            --api "${{ vars.MODELASSURE_API_URL }}" \
            --project "${{ vars.MODELASSURE_PROJECT_ID }}" \
            --version "${GITHUB_REF_NAME#v}" \
            --endpoint "https://sandbox.example.com/${GITHUB_REF_NAME#v}/decisions" \
            --timeout 1800
```

The job fails on exit codes 1, 2 and 3. A passing job means the build may go to human release
review, not that it may be deployed.

## API

| Method | Endpoint | Who |
|---|---|---|
| `POST` | `/v1/gate/runs` | pipeline key, Engineer, Admin |
| `GET` | `/v1/gate/runs/{id}?wait=0..60` | pipeline key (its project), any user |
| `POST` | `/v1/api-keys` | Admin |
| `GET` | `/v1/api-keys` | Admin |
| `POST` | `/v1/api-keys/{id}/revoke` | Admin |

`GET /v1/gate/runs/{id}` returns `status`, `finished`, `outcome`, `exit_code`, case counts,
fail and inconclusive reasons, flags, blocking findings and a link to the release review.

## Limits

- The ModelAssure API must be able to reach the sandbox endpoint, and the endpoint's host must be
  on the organisation's approved destinations.
- The gate always runs the full suite. Large suites against slow sandboxes take time; raise
  `--timeout` accordingly.
- Fault injection needs the dependency simulator that the template connection uses.
