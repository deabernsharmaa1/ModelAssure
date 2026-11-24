"""Shared fixtures. Integration tests start the fictional bank (baseline, candidate, dependency
simulator) as real HTTP processes on free ports."""
from __future__ import annotations

import os
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
