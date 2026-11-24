"""Calls a registered decision system and turns whatever happens into an Observation.

Every outcome is explicit (NFR-06): timeouts, connection failures, HTTP errors, malformed JSON,
schema violations and wrong deployed versions are recorded as errors, never as passes.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx

from modules.connectors.mapping import build_request, normalize_response
from modules.evaluation.engine import Observation


class RateLimiter:
    """Thread-safe token bucket (requests per second)."""

    def __init__(self, rps: float | None):
        self.rps = rps
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self) -> None:
        if not self.rps:
            return
        with self.lock:
            now = time.monotonic()
            at = max(now, self.next_at)
            self.next_at = at + 1.0 / self.rps
        if at > now:
            time.sleep(at - now)


class DecisionClient:
    def __init__(self, cfg: dict[str, Any], credential: str | None, recorded: dict[str, Any] | None = None):
        """cfg: the connection's locked snapshot (endpoint, auth_header, mapping, timeout_ms,
        max_concurrency, max_rps, expected_model_version, kind)."""
        self.cfg = cfg
        self.kind = cfg.get("kind", "REST")
        self.mapping = cfg.get("mapping") or {}
        self.expected_model_version = cfg.get("expected_model_version")
        self.recorded = recorded or {}
        self.semaphore = threading.BoundedSemaphore(max(1, int(cfg.get("max_concurrency") or 1)))
        self.limiter = RateLimiter(cfg.get("max_rps"))
        headers = {"Content-Type": "application/json", "User-Agent": "ModelAssure/1.0 (release assurance)"}
        if credential and cfg.get("auth_header"):
            headers[cfg["auth_header"]] = credential
        self.http = None
        if self.kind == "REST":
            self.http = httpx.Client(
                headers=headers, follow_redirects=False,
                timeout=httpx.Timeout(cfg.get("timeout_ms", 5000) / 1000.0),
                limits=httpx.Limits(max_connections=max(1, int(cfg.get("max_concurrency") or 1))),
            )

    def close(self) -> None:
        if self.http is not None:
            self.http.close()

    def call(self, canonical_input: Any, traceparent: str | None = None, case_id: str | None = None) -> Observation:
        if self.kind == "RECORDED":
            return self._recorded(case_id)
        body = build_request(self.mapping, canonical_input)
        headers = {"traceparent": traceparent} if traceparent else {}
        with self.semaphore:
            self.limiter.wait()
            t0 = time.perf_counter()
            try:
                r = self.http.post(self.cfg["endpoint"], content=json.dumps(body), headers=headers)
            except httpx.TimeoutException as e:
                return Observation(status="ERROR", error_type="TIMEOUT", error_detail=f"no response within {self.cfg.get('timeout_ms')} ms ({type(e).__name__})",
                                   raw_request=body, duration_ms=_ms(t0))
            except httpx.HTTPError as e:
                return Observation(status="ERROR", error_type="CONNECTION_ERROR", error_detail=str(e)[:500],
                                   raw_request=body, duration_ms=_ms(t0))
            duration = _ms(t0)
        return self._interpret(r.status_code, r.text, body, duration)

    def _interpret(self, status: int, text: str, body: Any, duration: float | None) -> Observation:
        obs = Observation(status="OK", http_status=status, raw_request=body, duration_ms=duration)
        try:
            parsed = json.loads(text) if text else None
            obs.raw_response = parsed
        except ValueError:
            parsed = None
            obs.raw_text = text[:4000]
        if status in (401, 403):
            obs.status, obs.error_type, obs.error_detail = "ERROR", "AUTH_ERROR", f"HTTP {status}: credentials rejected"
            return obs
        if status == 429:
            obs.status, obs.error_type, obs.error_detail = "ERROR", "RATE_LIMITED", "HTTP 429 from decision system"
            return obs
        if 300 <= status < 400:
            obs.status, obs.error_type, obs.error_detail = "ERROR", "HTTP_ERROR", f"HTTP {status}: redirects are not followed"
            return obs
        if status == 404:
            obs.status, obs.error_type, obs.error_detail = "ERROR", "HTTP_ERROR", "HTTP 404: endpoint not found"
            return obs
        if 400 <= status < 500:
            obs.status = "REJECTED"
            obs.error_detail = f"HTTP {status}"
            return obs
        if status >= 500:
            obs.status, obs.error_type, obs.error_detail = "ERROR", "HTTP_ERROR", f"HTTP {status}"
            return obs
        if parsed is None and obs.raw_text is not None:
            obs.status, obs.error_type, obs.error_detail = "ERROR", "MALFORMED_RESPONSE", "response body is not valid JSON"
            return obs
        normalized, errors, log = normalize_response(self.mapping, parsed)
        obs.transformations = log
        if errors:
            obs.status, obs.error_type, obs.error_detail = "ERROR", "SCHEMA_ERROR", "; ".join(errors)[:1000]
            return obs
        obs.normalized = normalized
        obs.trace_id = normalized.get("trace_id")
        if self.expected_model_version and normalized.get("model_version") != self.expected_model_version:
            obs.status, obs.error_type = "ERROR", "VERSION_MISMATCH"
            obs.error_detail = (f"endpoint reported model version {normalized.get('model_version')!r}, "
                                f"registered version expects {self.expected_model_version!r}")
        return obs

    def _recorded(self, case_id: str | None) -> Observation:
        if case_id is None or case_id not in self.recorded:
            return Observation(status="ERROR", error_type="NO_RECORDED_OUTPUT",
                               error_detail=f"no recorded output for case {case_id!r}")
        raw = self.recorded[case_id]
        return self._interpret(200, json.dumps(raw), None, None)


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 2)
