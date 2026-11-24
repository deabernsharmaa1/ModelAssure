"""ModelAssure ReleaseGate API (FastAPI modular monolith, spec §5)."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from common.errors import ApiError, api_error_handler
from settings import get_settings


def _check_production_safety(s) -> None:
    """Refuse to start a production deployment with development-only settings."""
    if s.environment != "production":
        return
    problems = []
    if s.auth_mode != "oidc":
        problems.append("MA_AUTH_MODE must be 'oidc' in production (dev login is for demos only)")
    if s.jwt_secret.startswith("change-me"):
        problems.append("MA_JWT_SECRET still has the development default")
    if not s.storage_encryption_key and s.storage_backend == "local":
        problems.append("MA_STORAGE_ENCRYPTION_KEY must be set")
    if problems:
        raise RuntimeError("unsafe production configuration: " + "; ".join(problems))


def create_app() -> FastAPI:
    s = get_settings()
    _check_production_safety(s)
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
    from modules.compliance.router import router as compliance_router
    from modules.connectors.router import router as connections_router
    from modules.findings.router import router as findings_router
    from modules.identity.router import router as identity_router
    from modules.policies.router import router as policies_router
    from modules.projects.router import router as projects_router
    from modules.reports.router import router as reports_router
    from modules.runner.router import router as runs_router
    from modules.scenarios.router import router as scenarios_router

    for r in (identity_router, projects_router, connections_router, policies_router, scenarios_router,
              runs_router, findings_router, reports_router, compliance_router, audit_router):
        app.include_router(r)

    @app.get("/health", tags=["system"])
    def health() -> dict:
        return {"status": "ok", "version": "1.0.0", "auth_mode": s.auth_mode}

    return app


app = create_app()
