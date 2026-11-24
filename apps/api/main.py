"""ModelAssure ReleaseGate API (FastAPI modular monolith, spec §5)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.errors import ApiError, api_error_handler
from settings import get_settings


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title="ModelAssure ReleaseGate API",
        version="1.0.0",
        description="Independent release assurance for financial decision systems (Version 1: SME lending). "
                    "ModelAssure supports human release decisions; it never authorises production deployment.",
    )
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in s.cors_origins.split(",") if o.strip()],
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

    from modules.audit.router import router as audit_router
    from modules.connectors.router import router as connections_router
    from modules.identity.router import router as identity_router
    from modules.projects.router import router as projects_router

    for r in (identity_router, projects_router, connections_router, audit_router):
        app.include_router(r)
    try:  # later phases register more routers
        from modules.registry import extra_routers
        for r in extra_routers():
            app.include_router(r)
    except ImportError:
        pass

    @app.get("/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok", "version": "1.0.0", "auth_mode": s.auth_mode}

    return app


app = create_app()
