"""One-command demonstration of ModelAssure's first commercially credible product (spec, final
build recommendation): three scenarios, investigated, fixed, retested, reviewed and reported.

  python scripts/demo.py --reset          # fresh database, seed users, run the full story
  python scripts/demo.py                  # reuse the database, add a new demo project

Starts the fictional bank locally, drives ModelAssure through its public API exactly as a
lender's team would, runs the worker in-process, and writes the reports to ./var/demo/.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "apps/api"), str(ROOT)]
os.environ.setdefault("MA_STORAGE_PATH", str(ROOT / "var/objects"))
os.environ.setdefault("MA_SECRET_STORE_PATH", str(ROOT / "var/secrets"))

from fastapi.testclient import TestClient  # noqa: E402

PASSWORD = "ModelAssure!2026"


class As:
    def __init__(self, client, token):
        self.c, self.h = client, {"Authorization": f"Bearer {token}"}

    def get(self, url, **kw):
        return self.c.get(url, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def post(self, url, **kw):
        return self.c.post(url, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def patch(self, url, **kw):
        return self.c.patch(url, headers={**self.h, **kw.pop("headers", {})}, **kw)


def say(msg: str = "") -> None:
    print(msg, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="drop and recreate the schema, then seed demo users")
    ap.add_argument("--out", default=str(ROOT / "var/demo"))
    args = ap.parse_args()

    from scripts.seed import seed
    from scripts.sim_processes import start_bank, stop

    if args.reset:
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "infrastructure/migrations"))
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
    seed(PASSWORD)

    from main import create_app
    from scripts.demo_setup import approve_policy_sets, create_standard_suite, queue_run, register_bank
    from workers.executor.main import drain

    sim, procs = start_bank()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        with TestClient(create_app()) as client:
            tokens: dict[str, str] = {}

            def as_(who: str) -> As:
                email = who if "@" in who else f"{who}@tanjung.example"
                if email not in tokens:
                    r = client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
                    r.raise_for_status()
                    tokens[email] = r.json()["access_token"]
                return As(client, tokens[email])

            def run_and_wait(run_id: str) -> dict:
                t0 = time.time()
                drain()
                run = as_("analyst").get(f"/v1/runs/{run_id}").json()
                run["_seconds"] = round(time.time() - t0, 1)
                return run

            say("== 1. Register the fictional bank's decision system (baseline 3.1.0, candidate 3.2.0, fix 3.2.1)")
            bank = register_bank(as_, sim, system_name=f"Tanjung SME Decision Engine {time.strftime('%H%M%S')}")
            for v, c in bank["connections"].items():
                r = as_("engineer").post(f"/v1/connections/{c['id']}/verify").json()
                say(f"   connection {v}: {r['status']}")
            say("== 2. Risk Manager authors policy 2026-06 and 2026-11; Reviewer approves (four-eyes)")
            policies = approve_policy_sets(as_, bank["project"]["id"])
            say("== 3. Analyst builds the standard release suite")
            suite = create_standard_suite(as_, bank["project"]["id"], [policies["2026-06"]["id"], policies["2026-11"]["id"]])
            say(f"   {suite['case_count']} cases: {suite['category_counts']}")

            say("== 4. Run the suite: baseline 3.1.0 vs candidate 3.2.0")
            run = run_and_wait(queue_run(as_, bank, policies, suite, "3.2.0")["run_id"])
            s = run["summary"]
            say(f"   run #{run['run_number']} finished in {run['_seconds']} s: {s['by_result']}")
            say(f"   assessment: {run['assessment']['outcome']} — {run['assessment']['recommendation']}")
            items = as_("analyst").get("/v1/findings", params={"run_id": run["id"]}).json()["items"]
            say("   findings:")
            for f in items:
                say(f"     F-{f['finding_number']:<3} {f['severity']:<8} {f['affected_count']:>4} cases  {f['title']}")

            say("== The three scenarios")
            pol1 = next(f for f in items if "POL-001" in f["rule_ids"])
            pol2 = next(f for f in items if "POL-002" in f["rule_ids"])
            say(f"   (1) bureau check fails → candidate approves anyway: F-{pol1['finding_number']}, {pol1['affected_count']} cases.")
            say(f"       Suspected cause: {pol1['suspected_cause']}")
            say(f"   (2) unresolved high-risk fraud alert overridden: F-{pol2['finding_number']}, {pol2['affected_count']} cases.")
            say(f"   (3) approved DSCR change 1.25 → 1.50: {s['changes']['expected']} changed decisions classified EXPECTED, no finding.")

            say("== 5. Reviewer cannot approve a failed release; requests remediation")
            r = as_("reviewer").post(f"/v1/runs/{run['id']}/reviews", json={"decision": "APPROVE_RELEASE", "rationale": "trying to approve"})
            say(f"   approve attempt → HTTP {r.status_code}")
            as_("reviewer").post(f"/v1/runs/{run['id']}/reviews", json={"decision": "REQUEST_REMEDIATION",
                                                                         "rationale": "Resolve the critical bureau and fraud findings, then retest."})
            say("== 6. Engineering fixes the defects (build 3.2.1) and retests the affected cases")
            eng = bank["users"]["engineer@tanjung.example"]["id"]
            for f in items:
                if f["type"] in ("POLICY_VIOLATION", "CHANGE_CONTROL_VIOLATION"):
                    for step in ({"owner_id": eng, "status": "INVESTIGATING"}, {"status": "FIX_PROPOSED"},
                                 {"status": "READY_FOR_RETEST", "comment": "Fixed in 3.2.1"}):
                        as_("engineer").patch(f"/v1/findings/{f['id']}", json=step).raise_for_status()
            rt = as_("analyst").post(f"/v1/runs/{run['id']}/retest", json={"candidate_version_id": bank["versions"]["3.2.1"]["id"]}).json()
            rtr = run_and_wait(rt["run_id"])
            say(f"   retest run #{rtr['run_number']} ({rt['cases']} cases): {rtr['summary']['by_result']}")
            for f in as_("analyst").get("/v1/findings", params={"run_id": run["id"], "type": "POLICY_VIOLATION"}).json()["items"]:
                say(f"     F-{f['finding_number']} now {f['status']}")

            say("== 7. Full suite on 3.2.1, then human release review")
            full = run_and_wait(queue_run(as_, bank, policies, suite, "3.2.1")["run_id"])
            say(f"   run #{full['run_number']}: {full['summary']['by_result']} → {full['assessment']['outcome']}")
            for fl in full["assessment"]["flags"]:
                say(f"     flag: {fl}")
            rv = as_("reviewer").post(f"/v1/runs/{full['id']}/reviews", json={
                "decision": "APPROVE_RELEASE",
                "rationale": "Critical bureau and fraud findings fixed and retested; unexpected DECLINE→REFER changes reviewed with Credit Risk."})
            say(f"   reviewer decision recorded (HTTP {rv.status_code}). ModelAssure does not deploy.")

            say("== 8. Reports")
            for label, rid in (("defective-3.2.0", run["id"]), ("fixed-3.2.1", full["id"])):
                for fmt in ("pdf", "json"):
                    r = as_("risk").get(f"/v1/runs/{rid}/report", params={"format": fmt})
                    path = out / f"run-{label}.{fmt}"
                    path.write_bytes(r.content)
                    say(f"   {path} ({len(r.content):,} bytes, sha256 {r.headers['X-Content-SHA256'][:12]}…)")
            say(f"== Audit chain: {as_('auditor').get('/v1/audit-events/verify').json()}")
    finally:
        stop(procs)


if __name__ == "__main__":
    main()
