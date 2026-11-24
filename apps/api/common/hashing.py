"""Canonical JSON and content hashing used for evidence integrity (NFR-07)."""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID


def _default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, UUID):
        return str(o)
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    raise TypeError(f"not JSON serialisable: {type(o)!r}")


def canonical_json(value: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace. Same value -> same bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_default, ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))
