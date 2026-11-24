from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, details: Any = None):
        self.status, self.code, self.message, self.details = status, code, message, details
        super().__init__(message)


def not_found(what: str) -> ApiError:
    return ApiError(404, "NOT_FOUND", f"{what} not found")


def conflict(message: str, details: Any = None) -> ApiError:
    return ApiError(409, "CONFLICT", message, details)


def invalid(message: str, details: Any = None) -> ApiError:
    return ApiError(422, "INVALID", message, details)


def forbidden(message: str = "You do not have permission to perform this action") -> ApiError:
    return ApiError(403, "FORBIDDEN", message)


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    body: dict[str, Any] = {"error": {"code": exc.code, "message": exc.message}}
    if exc.details is not None:
        body["error"]["details"] = exc.details
    return JSONResponse(body, status_code=exc.status)
