"""Shared fixtures. Integration tests start the fictional bank (baseline, candidate, dependency
simulator) as real HTTP processes on free ports."""
from __future__ import annotations

import os
import tempfile

# Integration tests always use a dedicated test database and temporary storage. These must be
# set before anything imports `settings` (it is cached).
_TMP = tempfile.mkdtemp(prefix="modelassure-test-")
os.environ.setdefault("MA_DATABASE_URL", "postgresql+psycopg://modelassure_app:modelassure_app@localhost:5432/modelassure_test")
os.environ.setdefault("MA_MIGRATION_DATABASE_URL", "postgresql+psycopg://modelassure:modelassure@localhost:5432/modelassure_test")
os.environ.setdefault("MA_STORAGE_PATH", os.path.join(_TMP, "objects"))
os.environ.setdefault("MA_SECRET_STORE_PATH", os.path.join(_TMP, "secrets"))
os.environ.setdefault("MA_ENVIRONMENT", "test")
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, proc: subprocess.Popen, timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"process for {url} exited early: {proc.stderr.read().decode() if proc.stderr else ''}")
        try:
            if httpx.get(url, timeout=0.5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"timed out waiting for {url}")


def start_uvicorn(app: str, port: int, env: dict[str, str] | None = None) -> subprocess.Popen:
    full_env = {**os.environ, "PYTHONPATH": f"{ROOT / 'apps/api'}:{ROOT}", **(env or {})}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app, "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=ROOT, env=full_env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    _wait(f"http://127.0.0.1:{port}/health", proc)
    return proc


@pytest.fixture(scope="session")
def simulators():
    dep_port, base_port, cand_port = free_port(), free_port(), free_port()
    procs = []
    try:
        procs.append(start_uvicorn("workers.simulators.dependency_mock.app:app", dep_port,
                                   {"DEPENDENCY_TIMEOUT_SLEEP_S": "1.0"}))
        env = {"DEPENDENCY_URL": f"http://127.0.0.1:{dep_port}", "DEPENDENCY_TIMEOUT_S": "0.3"}
        procs.append(start_uvicorn("decision_simulator.baseline_api.app:app", base_port, env))
        procs.append(start_uvicorn("decision_simulator.candidate_api.app:app", cand_port, env))
        yield {
            "dependency": f"http://127.0.0.1:{dep_port}",
            "baseline": f"http://127.0.0.1:{base_port}",
            "candidate": f"http://127.0.0.1:{cand_port}",
            "api_key": "sim-test-key", "control_key": "sim-control-key", "dep_control_key": "dep-control-key",
        }
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


# ---------------------------------------------------------------------------------------------
# Database + API fixtures
# ---------------------------------------------------------------------------------------------
PASSWORD = "Test-Password-1"


@pytest.fixture(scope="session")
def database():
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "infrastructure/migrations"))
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    from scripts.seed import seed

    orgs = seed(PASSWORD, os.environ["MA_MIGRATION_DATABASE_URL"])
    return {"orgs": orgs, "owner_url": os.environ["MA_MIGRATION_DATABASE_URL"]}


class Api:
    def __init__(self, client):
        self.client = client
        self._tokens: dict[str, str] = {}

    def token(self, email: str) -> str:
        if email not in self._tokens:
            r = self.client.post("/v1/auth/login", json={"email": email, "password": PASSWORD})
            assert r.status_code == 200, r.text
            self._tokens[email] = r.json()["access_token"]
        return self._tokens[email]

    def as_(self, who: str):
        email = who if "@" in who else f"{who}@tanjung.example"
        return _As(self.client, {"Authorization": f"Bearer {self.token(email)}"})


class _As:
    def __init__(self, client, headers):
        self.c, self.h = client, headers

    def get(self, url, **kw):
        return self.c.get(url, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def post(self, url, **kw):
        return self.c.post(url, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def patch(self, url, **kw):
        return self.c.patch(url, headers={**self.h, **kw.pop("headers", {})}, **kw)

    def put(self, url, **kw):
        return self.c.put(url, headers={**self.h, **kw.pop("headers", {})}, **kw)


@pytest.fixture(scope="session")
def api(database):
    from fastapi.testclient import TestClient

    from main import create_app

    with TestClient(create_app()) as client:
        yield Api(client)
