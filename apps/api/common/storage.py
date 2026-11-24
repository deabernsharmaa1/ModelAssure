"""Encrypted object storage for evidence, reports and datasets (spec §9, §12.2).

`local`  — files on disk, encrypted with Fernet (AES-128-CBC + HMAC-SHA256). Development/single host.
`s3`     — any S3-compatible store (AWS S3, MinIO) with server-side encryption requested on every put.

Callers always store a SHA-256 of the plaintext in the database, so tampering is detectable
independently of the storage backend (NFR-07).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from cryptography.fernet import Fernet

from common.hashing import canonical_json, sha256_hex
from settings import get_settings


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> str: ...
    def get(self, ref: str) -> bytes: ...
    def delete(self, ref: str) -> None: ...


def _dev_key(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_bytes(Fernet.generate_key())
        os.chmod(path, 0o600)
    return path.read_bytes().strip()


def encryption_key() -> bytes:
    s = get_settings()
    if s.storage_encryption_key:
        return s.storage_encryption_key.encode()
    if s.environment == "production":
        raise RuntimeError("MA_STORAGE_ENCRYPTION_KEY must be set in production")
    return _dev_key(Path(s.storage_path).parent / "dev-encryption.key")


class LocalEncryptedStorage:
    def __init__(self, root: str, key: bytes):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.fernet = Fernet(key)

    def _path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("invalid storage key")
        return p

    def put(self, key: str, data: bytes) -> str:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(self.fernet.encrypt(data))
        os.replace(tmp, p)
        return f"local:{key}"

    def get(self, ref: str) -> bytes:
        return self.fernet.decrypt(self._path(ref.removeprefix("local:")).read_bytes())

    def delete(self, ref: str) -> None:
        self._path(ref.removeprefix("local:")).unlink(missing_ok=True)


class S3Storage:
    def __init__(self, bucket: str, endpoint_url: str | None):
        import boto3

        self.bucket = bucket
        self.client = boto3.client("s3", endpoint_url=endpoint_url)

    def put(self, key: str, data: bytes) -> str:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ServerSideEncryption="AES256")
        return f"s3:{key}"

    def get(self, ref: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=ref.removeprefix("s3:"))["Body"].read()

    def delete(self, ref: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=ref.removeprefix("s3:"))


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        s = get_settings()
        if s.storage_backend == "s3":
            if not s.s3_bucket:
                raise RuntimeError("MA_S3_BUCKET is required for the s3 storage backend")
            _storage = S3Storage(s.s3_bucket, s.s3_endpoint_url)
        else:
            _storage = LocalEncryptedStorage(s.storage_path, encryption_key())
    return _storage


def put_json(key: str, value: Any) -> tuple[str, str, int]:
    """Store canonical JSON. Returns (ref, sha256, size)."""
    data = canonical_json(value).encode()
    return get_storage().put(key, data), sha256_hex(data), len(data)


def get_json(ref: str) -> Any:
    return json.loads(get_storage().get(ref))
