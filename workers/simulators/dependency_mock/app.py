"""Controlled dependency simulator (spec §8.2 "Fault injection — mocked dependency service").

Stands in for the credit bureau, fraud engine and identity verification service. A customer's
sandbox decision system can be pointed at this service so ModelAssure can inject faults
(timeouts, unavailability, malformed or empty responses) without touching any real provider.

GET  /bureau/{application_id}  /fraud/{application_id}  /identity/{application_id}
PUT  /control/scenarios        X-Control-Key — register faults/data per application id
DELETE /control/scenarios      X-Control-Key
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from decision_simulator.engine import default_bureau_score

CONTROL_KEY = os.environ.get("DEPENDENCY_CONTROL_KEY", "dep-control-key")
TIMEOUT_SLEEP_S = float(os.environ.get("DEPENDENCY_TIMEOUT_SLEEP_S", "2.0"))
FAULT_MODES = {"NONE", "TIMEOUT", "UNAVAILABLE", "MALFORMED", "MISSING_RESPONSE"}
MAX_SCENARIOS = 200_000

app = FastAPI(title="ModelAssure dependency simulator", version="1.0.0")
_scenarios: dict[str, dict[str, Any]] = {}

DEFAULTS = {
    "bureau": lambda aid: {"status": "AVAILABLE", "score": default_bureau_score(aid)},
    "fraud": lambda aid: {"status": "CLEAR"},
    "identity": lambda aid: {"status": "VERIFIED"},
}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "scenarios": len(_scenarios)}


@app.put("/control/scenarios")
async def register(body: dict[str, Any], x_control_key: str = Header(default="")) -> dict[str, Any]:
    if x_control_key != CONTROL_KEY:
        raise HTTPException(403, "bad control key")
    items = body.get("scenarios")
    if not isinstance(items, list):
        raise HTTPException(400, "scenarios must be a list")
    if len(_scenarios) + len(items) > MAX_SCENARIOS:
        _scenarios.clear()
    for s in items:
        key = s.get("key")
        faults = s.get("faults") or {}
        if not isinstance(key, str) or not key:
            raise HTTPException(400, "each scenario needs a key")
        bad = {k: v for k, v in faults.items() if k not in DEFAULTS or v not in FAULT_MODES}
        if bad:
            raise HTTPException(400, f"invalid faults {bad}")
        _scenarios[key] = {"faults": faults, "data": s.get("data") or {}}
    return {"registered": len(items)}


@app.delete("/control/scenarios")
async def clear(x_control_key: str = Header(default="")) -> dict[str, Any]:
    if x_control_key != CONTROL_KEY:
        raise HTTPException(403, "bad control key")
    _scenarios.clear()
    return {"registered": 0}


async def _serve(kind: str, application_id: str) -> Response:
    sc = _scenarios.get(application_id, {})
    mode = (sc.get("faults") or {}).get(kind, "NONE")
    if mode == "TIMEOUT":
        await asyncio.sleep(TIMEOUT_SLEEP_S)
        return JSONResponse({"status": "late"}, status_code=504)
    if mode == "UNAVAILABLE":
        return JSONResponse({"error": "service unavailable"}, status_code=503)
    if mode == "MALFORMED":
        return PlainTextResponse('{"status": "AVAIL', media_type="application/json")
    if mode == "MISSING_RESPONSE":
        return Response(status_code=200, media_type="application/json")
    body = (sc.get("data") or {}).get(kind) or DEFAULTS[kind](application_id)
    return JSONResponse(body)


@app.get("/bureau/{application_id}")
async def bureau(application_id: str) -> Response:
    return await _serve("bureau", application_id)


@app.get("/fraud/{application_id}")
async def fraud(application_id: str) -> Response:
    return await _serve("fraud", application_id)


@app.get("/identity/{application_id}")
async def identity(application_id: str) -> Response:
    return await _serve("identity", application_id)
