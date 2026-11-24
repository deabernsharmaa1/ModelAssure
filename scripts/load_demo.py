"""Load the fictional-bank demo into a RUNNING ModelAssure stack through its HTTP API, and run
the defective release candidate so the UI has a complete story to explore.

Docker Compose (from the repo root, after `docker compose up -d --build`):
    docker compose run --rm migrate python scripts/load_demo.py --api http://api:8000 \
        --baseline http://simulator-baseline:8101 --candidate http://simulator-candidate:8102 \
        --dependency http://dependency-simulator:8200

Local processes (see README):
    python scripts/load_demo.py

Writes "<project_id> <run_id>" to --ids-file (used by scripts/ui_smoke.py).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "apps/api"), str(ROOT)]

import httpx  # noqa: E402

from scripts.demo_setup import approve_policy_sets, create_standard_suite, queue_run, register_bank  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--baseline", default="http://127.0.0.1:8101")
    ap.add_argument("--candidate", default="http://127.0.0.1:8102")
    ap.add_argument("--dependency", default="http://127.0.0.1:8200")
    ap.add_argument("--password", default="ModelAssure!2026")
    ap.add_argument("--system-name", default=f"Tanjung SME Decision Engine {time.strftime('%Y%m%d-%H%M%S')}")
    ap.add_argument("--ids-file", default="/tmp/ui_ids.txt")
    ap.add_argument("--reset", action="store_true", help="DESTRUCTIVE: recreate the schema and seed users first (needs direct DB access)")
    a = ap.parse_args()

    if a.reset:
        from alembic import command
        from alembic.config import Config

        from scripts.seed import seed

        cfg = Config(str(ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(ROOT / "infrastructure/migrations"))
        command.downgrade(cfg, "base")
        command.upgrade(cfg, "head")
        seed(a.password)

    c = httpx.Client(base_url=a.api, timeout=60)
    tokens: dict[str, str] = {}

    class As:
        def __init__(self, h): self.h = h
        def get(self, u, **k): return c.get(u, headers={**self.h, **k.pop("headers", {})}, **k)
        def post(self, u, **k): return c.post(u, headers={**self.h, **k.pop("headers", {})}, **k)
        def patch(self, u, **k): return c.patch(u, headers={**self.h, **k.pop("headers", {})}, **k)

    def as_(who: str) -> As:
        email = who if "@" in who else f"{who}@tanjung.example"
        if email not in tokens:
            r = c.post("/v1/auth/login", json={"email": email, "password": a.password})
            r.raise_for_status()
            tokens[email] = r.json()["access_token"]
        return As({"Authorization": f"Bearer {tokens[email]}"})

    sim = {"baseline": a.baseline, "candidate": a.candidate, "dependency": a.dependency,
           "api_key": "sim-test-key", "dep_control_key": "dep-control-key"}
    bank = register_bank(as_, sim, system_name=a.system_name)
    for v, conn in bank["connections"].items():
        print(f"connection {v}: {as_('engineer').post(f'/v1/connections/{conn['id']}/verify').json()['status']}")
    policies = approve_policy_sets(as_, bank["project"]["id"])
    suite = create_standard_suite(as_, bank["project"]["id"], [policies["2026-06"]["id"], policies["2026-11"]["id"]])
    print(f"suite: {suite['case_count']} cases")
    queued = queue_run(as_, bank, policies, suite, "3.2.0")
    print("waiting for the worker to execute the run…")
    for _ in range(600):
        run = as_("analyst").get(f"/v1/runs/{queued['run_id']}").json()
        if run["status"] in ("COMPLETED", "FAILED", "CANCELLED"):
            break
        time.sleep(1)
    print(f"run #{run['run_number']} {run['status']}: {run.get('summary', {}).get('by_result')} → {(run.get('assessment') or {}).get('outcome')}")
    Path(a.ids_file).write_text(f"{bank['project']['id']} {queued['run_id']}")
    print(f"project {bank['project']['id']} — open http://localhost:3000/projects/{bank['project']['id']}")


if __name__ == "__main__":
    main()
