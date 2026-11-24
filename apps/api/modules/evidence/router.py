"""Evidence packs and the organisation's signing key."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.deps import get_or_404, row, tenant_db
from common.errors import conflict, not_found
from common.hashing import sha256_hex
from common.storage import get_storage
from db.models import Artifact, SigningKey, TestRun
from modules.audit.service import record_for
from modules.evidence.pack import build_pack
from modules.evidence.signing import active_key, rotate
from modules.identity.auth import Principal, require

router = APIRouter(prefix="/v1", tags=["evidence packs"])


class PackIn(BaseModel):
    case_evidence: Literal["none", "failing", "all"] = "none"


def _pack_row(a: Artifact) -> dict:
    return {k: v for k, v in row(a).items() if k != "storage_ref"}


@router.post("/runs/{run_id}/evidence-packs", status_code=201)
def create_pack(run_id: str, body: PackIn, p: Principal = Depends(require("evidence_pack.create")),
                db: Session = Depends(tenant_db)):
    """Create a signed evidence pack for a completed run. `case_evidence` adds raw request and
    response evidence for failing cases or all cases; it may contain applicant data, so the
    default is none."""
    run = get_or_404(db, TestRun, run_id, "run")
    created_key = active_key(db, run.organization_id) is None
    a, manifest = build_pack(db, run, p, body.case_evidence)
    if created_key:
        record_for(db, p, "signing_key.create", "signing_key", manifest["signing"]["key_fingerprint"],
                   {"algorithm": "Ed25519"})
    record_for(db, p, "evidence_pack.create", "test_run", run.id,
               {"run_id": str(run.id), "version": a.version, "pack_id": manifest["pack_id"],
                "manifest_sha256": a.metadata_["manifest_sha256"], "key_fingerprint": manifest["signing"]["key_fingerprint"],
                "case_evidence": body.case_evidence, "sha256": a.checksum})
    db.commit()
    return {"pack": _pack_row(a), "manifest": manifest}


@router.get("/runs/{run_id}/evidence-packs")
def list_packs(run_id: str, p: Principal = Depends(require("report.read")), db: Session = Depends(tenant_db)):
    run = get_or_404(db, TestRun, run_id, "run")
    return [_pack_row(a) for a in db.scalars(select(Artifact).where(Artifact.run_id == run.id, Artifact.type == "EVIDENCE_PACK")
                                             .order_by(Artifact.version.desc()))]


@router.get("/runs/{run_id}/evidence-packs/{version}")
def download_pack(run_id: str, version: int, p: Principal = Depends(require("evidence_pack.create")),
                  db: Session = Depends(tenant_db)):
    run = get_or_404(db, TestRun, run_id, "run")
    a = db.scalars(select(Artifact).where(Artifact.run_id == run.id, Artifact.type == "EVIDENCE_PACK",
                                          Artifact.version == version)).first()
    if a is None:
        raise not_found("evidence pack")
    data = get_storage().get(a.storage_ref)
    if sha256_hex(data) != a.checksum:
        raise conflict("stored evidence pack failed its integrity check")
    record_for(db, p, "evidence_pack.download", "test_run", run.id, {"run_id": str(run.id), "version": version})
    db.commit()
    return Response(data, media_type="application/zip", headers={
        "X-Content-SHA256": a.checksum,
        "Content-Disposition": f'attachment; filename="modelassure-run-{run.run_number}-evidence-pack-v{version}.zip"'})


def _key_row(k: SigningKey) -> dict:
    return {"algorithm": k.algorithm, "fingerprint": k.fingerprint, "public_key_pem": k.public_key_pem,
            "status": k.status, "created_at": k.created_at.isoformat(), "retired_at": k.retired_at.isoformat() if k.retired_at else None}


@router.get("/signing-keys")
def list_keys(p: Principal = Depends(require("report.read")), db: Session = Depends(tenant_db)):
    """Public keys only. Share the active fingerprint with recipients through a separate channel
    so they can check packs with `verify_pack.py --expect-fingerprint`."""
    keys = db.scalars(select(SigningKey).order_by(SigningKey.created_at.desc()))
    return [_key_row(k) for k in keys]


@router.post("/signing-keys/rotate", status_code=201)
def rotate_key(p: Principal = Depends(require("signing_key.manage")), db: Session = Depends(tenant_db)):
    """Retire the active key and create a new one. Packs signed earlier stay verifiable: each
    carries its own public key, and retired keys remain listed."""
    old, new = rotate(db, p.org_id, p.user_id)
    record_for(db, p, "signing_key.rotate", "signing_key", new.fingerprint,
               {"retired": old.fingerprint if old else None, "active": new.fingerprint})
    db.commit()
    return {"active": _key_row(new), "retired": _key_row(old) if old else None}
