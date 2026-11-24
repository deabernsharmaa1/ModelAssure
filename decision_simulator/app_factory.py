"""HTTP API of the fictional lender's decision system (vendor-style field names on purpose, so
ModelAssure's field mapping is exercised).

POST /{build}/decisions         X-API-Key required
GET  /health
POST /control/api-faults        X-Control-Key required — simulator-only switch used by QA tests to make
                                specific cases time out / return malformed JSON / HTTP 500
DELETE /control/api-faults
"""
from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any, Literal

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from decision_simulator.engine import BUILDS, STATUS_OUT, decide, default_bureau_score

API_KEY = os.environ.get("SIMULATOR_API_KEY", "sim-test-key")
CONTROL_KEY = os.environ.get("SIMULATOR_CONTROL_KEY", "sim-control-key")
DEPENDENCY_URL = os.environ.get("DEPENDENCY_URL", "http://127.0.0.1:8200")
DEPENDENCY_TIMEOUT_S = float(os.environ.get("DEPENDENCY_TIMEOUT_S", "0.4"))


class Bureau(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    score: int | None = Field(default=None, ge=300, le=900)


class Fraud(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    status: Literal["CLEAR", "MEDIUM", "HIGH"]
    alertResolved: bool | None = None


class Identity(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    status: Literal["VERIFIED", "PENDING", "FAILED"]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    applicationId: str = Field(min_length=1, max_length=64)
    productCode: Literal["SME_WORKING_CAPITAL", "SME_TERM_LOAN"]
    requestedAmount: float = Field(gt=0, le=5_000_000)
    currency: Literal["MYR"]
    monthlyCashAvailable: float | None = Field(default=None, ge=0)
    monthlyDebtService: float | None = Field(default=None, ge=0)
    bureau: Bureau | None = None
    fraud: Fraud | None = None
    identity: Identity | None = None


async def _fetch(client: httpx.AsyncClient, kind: str, application_id: str) -> dict[str, Any]:
    try:
        r = await client.get(f"{DEPENDENCY_URL}/{kind}/{application_id}", timeout=DEPENDENCY_TIMEOUT_S)
    except httpx.TimeoutException:
        return {"status": "TIMEOUT"}
    except httpx.HTTPError:
        return {"status": "UNAVAILABLE" if kind == "bureau" else "TIMEOUT"}
    if r.status_code >= 400:
        return {"status": "UNAVAILABLE" if kind == "bureau" else "TIMEOUT"}
    try:
        body = r.json()
        if not isinstance(body, dict) or "status" not in body:
            raise ValueError
        return body
    except ValueError:
        # malformed or empty dependency response: treated as unavailable data
        return {"status": "UNAVAILABLE" if kind == "bureau" else "TIMEOUT"}


def create_app(builds: list[str], role: str) -> FastAPI:
    app = FastAPI(title=f"Tanjung Kredit decision API ({role})", version=",".join(builds))
    api_faults: dict[str, dict[str, str]] = {}  # applicationId -> {"mode":..., "build":...}
    client_holder: dict[str, httpx.AsyncClient] = {}

    def _client() -> httpx.AsyncClient:
        c = client_holder.get("c")
        if c is None or c.is_closed:
            c = client_holder["c"] = httpx.AsyncClient()
        return c

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "role": role, "builds": builds}

    @app.post("/control/api-faults")
    async def set_faults(body: dict[str, Any], x_control_key: str = Header(default="")) -> dict[str, Any]:
        if x_control_key != CONTROL_KEY:
            raise HTTPException(403, "bad control key")
        mode = body.get("mode")
        if mode not in ("timeout", "malformed_json", "http_500", "unmapped_decision"):
            raise HTTPException(400, "mode must be timeout|malformed_json|http_500|unmapped_decision")
        for cid in body.get("application_ids", []):
            api_faults[cid] = {"mode": mode, "build": body.get("build", "*")}
        return {"active": len(api_faults)}

    @app.delete("/control/api-faults")
    async def clear_faults(x_control_key: str = Header(default="")) -> dict[str, Any]:
        if x_control_key != CONTROL_KEY:
            raise HTTPException(403, "bad control key")
        api_faults.clear()
        return {"active": 0}

    @app.post("/{build}/decisions")
    async def decisions(build: str, request: Request, x_api_key: str = Header(default="")):
        if x_api_key != API_KEY:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if build not in builds:
            return JSONResponse({"error": f"unknown build {build}"}, status_code=404)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"error": "request body is not valid JSON"}, status_code=400)
        try:
            req = DecisionRequest.model_validate(payload)
        except ValidationError as e:
            return JSONResponse(
                {"error": "invalid request", "details": [
                    {"loc": ".".join(str(p) for p in err["loc"]), "msg": err["msg"]} for err in e.errors()
                ]},
                status_code=422,
            )
        fault = api_faults.get(req.applicationId)
        if fault and fault["build"] in ("*", build):
            if fault["mode"] == "timeout":
                await asyncio.sleep(30)
            if fault["mode"] == "malformed_json":
                return PlainTextResponse('{"applicationId": "' + req.applicationId + '", "finalStatus": APPR', media_type="application/json")
            if fault["mode"] == "http_500":
                return JSONResponse({"error": "internal error"}, status_code=500)

        b = BUILDS[build]
        client = _client()
        reqd = req.model_dump(exclude_none=True)
        tasks = {}
        if req.bureau is None:
            tasks["bureau"] = _fetch(client, "bureau", req.applicationId)
        if req.fraud is None:
            tasks["fraud"] = _fetch(client, "fraud", req.applicationId)
        if req.identity is None:
            tasks["identity"] = _fetch(client, "identity", req.applicationId)
        fetched = dict(zip(tasks, await asyncio.gather(*tasks.values()))) if tasks else {}
        bureau = reqd.get("bureau") or fetched["bureau"]
        if bureau.get("status") == "AVAILABLE" and bureau.get("score") is None:
            bureau = {**bureau, "score": default_bureau_score(req.applicationId)}
        fraud = reqd.get("fraud") or fetched["fraud"]
        identity = reqd.get("identity") or fetched["identity"]

        decision, pd, reasons = decide(b, reqd, bureau, fraud, identity)
        status_out = STATUS_OUT[decision]
        if fault and fault["mode"] == "unmapped_decision" and fault["build"] in ("*", build):
            status_out = "PENDING_SOMETHING"

        tp = request.headers.get("traceparent", "")
        trace_id = tp.split("-")[1] if tp.count("-") >= 3 else uuid.uuid4().hex
        return {
            "applicationId": req.applicationId,
            "finalStatus": status_out,
            "probabilityOfDefault": pd,
            "modelVersion": b.model_version,
            "policyVersion": b.policy_version,
            "reasonCodes": reasons,
            "traceId": trace_id,
        }

    return app
