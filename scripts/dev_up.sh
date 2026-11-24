#!/usr/bin/env bash
# Start the whole stack as local processes (no Docker): fictional bank, API, worker, web.
# Requires PostgreSQL with the roles from README "Run locally", Python deps and `npm ci` in apps/web.
# Logs go to var/logs/. Stop everything with scripts/dev_down.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
mkdir -p var/logs
export PYTHONPATH="$ROOT/apps/api:$ROOT"
export MA_STORAGE_PATH="${MA_STORAGE_PATH:-$ROOT/var/objects}"
export MA_SECRET_STORE_PATH="${MA_SECRET_STORE_PATH:-$ROOT/var/secrets}"

alembic upgrade head
python3 scripts/seed.py

# setsid + </dev/null: fully detach so the services never hold this shell's terminal or pipes.
start() { local name=$1; shift; setsid nohup "$@" < /dev/null > "var/logs/$name.log" 2>&1 & echo $! > "var/logs/$name.pid"; }
DEPENDENCY_TIMEOUT_SLEEP_S=1.0 start dependency python3 -m uvicorn workers.simulators.dependency_mock.app:app --port 8200
DEPENDENCY_URL=http://127.0.0.1:8200 DEPENDENCY_TIMEOUT_S=0.3 start baseline python3 -m uvicorn decision_simulator.baseline_api.app:app --port 8101
DEPENDENCY_URL=http://127.0.0.1:8200 DEPENDENCY_TIMEOUT_S=0.3 start candidate python3 -m uvicorn decision_simulator.candidate_api.app:app --port 8102
start api python3 -m uvicorn main:app --app-dir apps/api --port 8000
start worker python3 -m workers.executor.main
if [ ! -d apps/web/.next ]; then (cd apps/web && npm run build); fi
(cd apps/web && NEXT_PUBLIC_API_URL=http://localhost:8000 setsid nohup npx next start -p 3000 < /dev/null > "$ROOT/var/logs/web.log" 2>&1 & echo $! > "$ROOT/var/logs/web.pid")
sleep 4
curl -fsS localhost:8000/health >/dev/null && echo "API     http://localhost:8000/docs"
echo "Web     http://localhost:3000   (password ModelAssure!2026)"
echo "Load the demo:  python3 scripts/load_demo.py"
