"""Runtime configuration (12-factor; every value can be set by environment variable)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MA_", env_file=".env", extra="ignore")

    # Database: the API and workers connect as a NON-owner role so PostgreSQL row-level security
    # applies. Migrations run as the owner role.
    database_url: str = "postgresql+psycopg://modelassure_app:modelassure_app@localhost:5432/modelassure"
    migration_database_url: str = "postgresql+psycopg://modelassure:modelassure@localhost:5432/modelassure"

    environment: str = "development"  # development | test | production

    # Identity. "dev" = local password login with seeded users (NOT for production).
    # "oidc" = validate bearer tokens from an OAuth 2.0 / OIDC identity provider.
    auth_mode: str = "dev"
    jwt_secret: str = "change-me-dev-only-secret-at-least-32-bytes!"
    jwt_ttl_minutes: int = 480
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    require_mfa_for_privileged: bool = False  # set true with OIDC; checks the token's `amr` claim

    # Secrets and evidence storage
    secret_store_path: str = "./var/secrets"
    storage_backend: str = "local"  # local | s3
    storage_path: str = "./var/objects"
    storage_encryption_key: str | None = None  # Fernet key; generated into var/ if unset (dev only)
    s3_bucket: str | None = None
    s3_endpoint_url: str | None = None

    # Egress control (spec §12.2 "Testing isolation")
    allowed_test_hosts: str = "localhost,127.0.0.1,simulator-baseline,simulator-candidate,dependency-simulator"
    max_connection_concurrency: int = 20
    default_connection_concurrency: int = 5  # NFR-03

    # Worker
    worker_lease_seconds: int = 60
    worker_poll_seconds: float = 1.0

    cors_origins: str = "http://localhost:3000"

    @property
    def allowed_hosts(self) -> set[str]:
        return {h.strip().lower() for h in self.allowed_test_hosts.split(",") if h.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
