#!/usr/bin/env python3
"""ModelAssure release gate for CI/CD pipelines. Standard library only.

    MODELASSURE_API_KEY=mak_... python3 modelassure_gate.py \\
        --api https://modelassure.example --project <project id> \\
        --version 3.2.1 --endpoint https://sandbox.vendor.example/3.2.1/decisions \\
        [--expected-model-version 3.2.1] [--commit $GIT_SHA] [--timeout 1800]

Exit codes:
    0  PASS          the defined tests passed (not a release authorisation)
    1  FAIL          the release criteria were violated
    2  INCONCLUSIVE  evidence incomplete (errors, inconclusive cases, or a partial suite)
    3  error         the gate could not run (bad key, endpoint failed verification, run failed, timeout)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

EXIT_ERROR = 3


class GateError(Exception):
    pass


def _call(api: str, key: str, method: str, path: str, body: dict | None = None, headers: dict | None = None,
          timeout: float = 90) -> dict[str, Any]:
    req = urllib.request.Request(f"{api.rstrip('/')}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                          "Accept": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", {})
        except ValueError:
            err = {}
        detail = err.get("message") or e.reason
        if err.get("details"):
            detail += "\n" + json.dumps(err["details"], indent=2)
        raise GateError(f"HTTP {e.code} {err.get('code', '')}: {detail}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise GateError(f"cannot reach {api}: {e}") from None


def run_gate(api: str, key: str, project: str, version: str, endpoint: str | None, expected: str | None,
             commit: str | None, timeout: int, poll: int, out=sys.stdout) -> int:
    source = {k: v for k, v in {"commit": commit, "ref": os.environ.get("GITHUB_REF"),
                                "pipeline_url": _pipeline_url()}.items() if v}
    body = {"project_id": project, "candidate_version": version, "candidate_endpoint": endpoint,
            "expected_model_version": expected, "source": source}
    idem = f"gate-{project}-{version}-{commit or 'nocommit'}"
    started = _call(api, key, "POST", "/v1/gate/runs", body, {"Idempotency-Key": idem})
    rid = started["run_id"]
    print(f"ModelAssure run #{started['run_number']} queued: {started.get('baseline_version') or 'baseline'} → {version}", file=out)
    deadline = time.monotonic() + timeout
    while True:
        r = _call(api, key, "GET", f"/v1/gate/runs/{rid}?wait={min(poll, 60)}")
        if r["finished"]:
            break
        if time.monotonic() > deadline:
            print(f"Timed out after {timeout}s (run {r['status']}, {r.get('progress', {}).get('percent', 0)}%).", file=out)
            return EXIT_ERROR
        print(f"  {r['status']} {r.get('progress', {}).get('percent', 0)}%", file=out)
    print(f"\nResult: {r['outcome'] or r['status']}   cases: {r.get('results')}", file=out)
    for title, items in (("Fails the release criteria", r["fail_reasons"]), ("Inconclusive", r["inconclusive_reasons"]),
                         ("Flagged for review", r["flags"])):
        if items:
            print(f"{title}:", file=out)
            for i in items:
                print(f"  - {i}", file=out)
    print(f"\nRelease review: {r['release_review_url']}", file=out)
    print(r["statement"], file=out)
    return r["exit_code"] if r["exit_code"] is not None else EXIT_ERROR


def _pipeline_url() -> str | None:
    if os.environ.get("GITHUB_SERVER_URL") and os.environ.get("GITHUB_RUN_ID"):
        return f"{os.environ['GITHUB_SERVER_URL']}/{os.environ.get('GITHUB_REPOSITORY', '')}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    return os.environ.get("CI_PIPELINE_URL")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the ModelAssure release gate for a new build.")
    ap.add_argument("--api", default=os.environ.get("MODELASSURE_API_URL"), help="ModelAssure API base URL")
    ap.add_argument("--project", default=os.environ.get("MODELASSURE_PROJECT_ID"), required=False)
    ap.add_argument("--version", required=True, help="version of the new build, e.g. 3.2.1")
    ap.add_argument("--endpoint", help="sandbox decision endpoint of the new build")
    ap.add_argument("--expected-model-version", help="model version the endpoint must report (default: --version)")
    ap.add_argument("--commit", default=os.environ.get("GITHUB_SHA"))
    ap.add_argument("--timeout", type=int, default=1800, help="seconds to wait for the run (default 1800)")
    ap.add_argument("--poll", type=int, default=30, help="seconds per status request (max 60)")
    a = ap.parse_args(argv)
    key = os.environ.get("MODELASSURE_API_KEY")
    if not (a.api and a.project and key):
        print("Set --api, --project and the MODELASSURE_API_KEY environment variable.", file=sys.stderr)
        return EXIT_ERROR
    try:
        return run_gate(a.api, key, a.project, a.version, a.endpoint, a.expected_model_version, a.commit, a.timeout, a.poll)
    except GateError as e:
        print(f"ModelAssure gate error: {e}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
