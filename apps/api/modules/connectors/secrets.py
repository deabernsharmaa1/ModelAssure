"""Credential storage. API credentials are never stored in application tables (spec §12.2);
the database keeps only an opaque reference.

  local:<id>   encrypted file in MA_SECRET_STORE_PATH (development / single host)
  env:<NAME>   read from an environment variable injected by the platform's secret manager
               (e.g. Kubernetes secrets, AWS Secrets Manager / Vault agents)
"""
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

from common.storage import encryption_key
from settings import get_settings


class SecretError(ValueError):
    pass


def _dir() -> Path:
    p = Path(get_settings().secret_store_path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def store_secret(value: str) -> str:
    if not value:
        raise SecretError("empty secret")
    sid = uuid.uuid4().hex
    path = _dir() / f"{sid}.secret"
    path.write_bytes(Fernet(encryption_key()).encrypt(value.encode()))
    os.chmod(path, 0o600)
    return f"local:{sid}"


def resolve_secret(ref: str | None) -> str | None:
    if not ref:
        return None
    if ref.startswith("env:"):
        name = ref[4:]
        if not re.fullmatch(r"[A-Z0-9_]+", name):
            raise SecretError("invalid env secret name")
        value = os.environ.get(name)
        if value is None:
            raise SecretError(f"secret {ref} is not available")
        return value
    if ref.startswith("local:"):
        sid = ref[6:]
        if not re.fullmatch(r"[0-9a-f]{32}", sid):
            raise SecretError("invalid secret reference")
        path = _dir() / f"{sid}.secret"
        if not path.exists():
            raise SecretError(f"secret {ref} is not available")
        return Fernet(encryption_key()).decrypt(path.read_bytes()).decode()
    raise SecretError("unsupported secret reference")
