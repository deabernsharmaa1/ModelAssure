"""Reset the development database and create a demo project with one completed defective run,
through the HTTP API of a running stack (API :8000, worker, simulators :8101/:8102/:8200).
Used before scripts/ui_smoke.py. DESTROYS the development database contents."""
import sys, time, httpx
sys.path[:0] = ["apps/api", "."]
from scripts.seed import seed
from scripts.demo_setup import register_bank, approve_policy_sets, create_standard_suite, queue_run
from alembic import command
from alembic.config import Config
cfg = Config("alembic.ini"); cfg.set_main_option("script_location", "infrastructure/migrations")
command.downgrade(cfg, "base"); command.upgrade(cfg, "head")
seed("ModelAssure!2026")
c = httpx.Client(base_url="http://localhost:8000", timeout=60)
tok = {}
class As:
    def __init__(s, h): s.h = h
    def get(s, u, **k): return c.get(u, headers={**s.h, **k.pop("headers", {})}, **k)
    def post(s, u, **k): return c.post(u, headers={**s.h, **k.pop("headers", {})}, **k)
    def patch(s, u, **k): return c.patch(u, headers={**s.h, **k.pop("headers", {})}, **k)
def as_(w):
    e = w if "@" in w else f"{w}@tanjung.example"
    if e not in tok: tok[e] = c.post("/v1/auth/login", json={"email": e, "password": "ModelAssure!2026"}).json()["access_token"]
    return As({"Authorization": f"Bearer {tok[e]}"})
sim = {"baseline": "http://127.0.0.1:8101", "candidate": "http://127.0.0.1:8102", "dependency": "http://127.0.0.1:8200",
       "api_key": "sim-test-key", "dep_control_key": "dep-control-key"}
bank = register_bank(as_, sim, system_name="Tanjung SME Decision Engine")
for v, conn in bank["connections"].items(): print(v, as_("engineer").post(f"/v1/connections/{conn['id']}/verify").json()["status"])
pol = approve_policy_sets(as_, bank["project"]["id"])
suite = create_standard_suite(as_, bank["project"]["id"], [pol["2026-06"]["id"], pol["2026-11"]["id"]])
r = queue_run(as_, bank, pol, suite, "3.2.0")
for _ in range(120):
    run = as_("analyst").get(f"/v1/runs/{r['run_id']}").json()
    if run["status"] == "COMPLETED": break
    time.sleep(1)
print(run["run_number"], run["status"], run["summary"]["by_result"], run["assessment"]["outcome"])
open("/tmp/ui_ids.txt","w").write(f"{bank['project']['id']} {r['run_id']}")
