"""Signed evidence packs: one file a vendor (or a lender's model-risk team) can hand to a bank,
auditor or supervisor, which the recipient can verify offline.

A pack is a zip holding a fresh report version (JSON + PDF), the outcomes CSV, the locked run
configuration (secrets removed), findings, reviews, the regulatory mapping, the audit events for
the run, optionally the raw evidence of selected cases, plus `manifest.json` (SHA-256 of every
file) signed with the organisation's Ed25519 key, the public key, a standalone verifier and
VERIFY.md."""
from __future__ import annotations

import copy
import io
import json
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from common.deps import row, to_json
from common.errors import conflict
from common.hashing import canonical_json, sha256_hex
from common.storage import get_storage
from db.models import Artifact, AuditEvent, Finding, Organization, Project, Review, TestExecution, TestRun
from modules.audit.service import _payload
from modules.compliance.mapping import evaluate as regulatory_mapping
from modules.evidence.signing import get_or_create_key, sign
from modules.evidence.verify_pack import FORMAT
from modules.identity.auth import Principal

CaseEvidence = Literal["none", "failing", "all"]
MAX_CASES = 5000
VERIFIER = Path(__file__).with_name("verify_pack.py")


def _json(value: Any) -> bytes:
    return (json.dumps(to_json(value), indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


def redacted_configuration(snapshot: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The run's locked configuration without secret references or internal network addresses.
    Each address is replaced by its SHA-256 so the issuer can show which endpoint was tested."""
    s = copy.deepcopy(snapshot)
    removed = []
    for side, c in (s.get("connections") or {}).items():
        for k in ("secret_ref", "dependency_secret_ref"):
            if c.pop(k, None) is not None:
                removed.append(f"connections.{side}.{k}")
        for k in ("endpoint", "dependency_control_url"):
            if c.get(k):
                c[f"{k}_sha256"] = sha256_hex(c.pop(k))
                removed.append(f"connections.{side}.{k}")
    return s, removed


def _audit_subset(db: Session, run: TestRun, finding_ids: list[str]) -> list[dict[str, Any]]:
    ids = [str(run.id), *finding_ids, *(str(x) for x in (run.policy_set_id, run.baseline_policy_set_id, run.suite_id) if x)]
    q = (select(AuditEvent).where(AuditEvent.organization_id == run.organization_id,
                                  or_(AuditEvent.resource_id.in_(ids), AuditEvent.details["run_id"].astext == str(run.id)))
         .order_by(AuditEvent.seq))
    return [{"payload": _payload(e), "prev_hash": e.prev_hash, "hash": e.hash} for e in db.scalars(q)]


def _case_evidence(db: Session, run: TestRun, mode: CaseEvidence) -> tuple[dict[str, bytes], dict[str, Any]]:
    if mode == "none":
        return {}, {"mode": "none"}
    q = select(TestExecution).where(TestExecution.run_id == run.id).order_by(TestExecution.case_key)
    if mode == "failing":
        q = q.where(TestExecution.result.in_(["FAIL", "ERROR", "INCONCLUSIVE"]))
    execs = list(db.scalars(q.limit(MAX_CASES + 1)))
    if len(execs) > MAX_CASES:
        raise conflict(f"more than {MAX_CASES} cases selected; choose 'failing' or export in parts")
    files, purged, altered = {}, [], []
    storage = get_storage()
    for e in execs:
        try:
            raw = storage.get(e.evidence_ref)
        except Exception:  # removed under the retention policy
            purged.append(e.case_key)
            continue
        if sha256_hex(raw) != e.evidence_hash:
            altered.append(e.case_key)
            continue
        files[f"cases/{e.case_key}.json"] = raw
    if altered:
        raise conflict(f"stored evidence failed its integrity check for {altered[:5]}; the pack was not created")
    return files, {"mode": mode, "included": len(files), "purged_under_retention": purged}


def _verify_md(manifest: dict[str, Any]) -> bytes:
    run = manifest["run"]
    return f"""# ModelAssure evidence pack

Issuer: **{manifest['issuer']['organization']}**
Run #{run['run_number']}: {run['baseline_version']} → {run['candidate_version']} ({run['project']})
Release assessment at signing: **{run['assessment']}**
Signed: {manifest['created_at']}
Signing key fingerprint (SHA-256): `{manifest['signing']['key_fingerprint']}`

## Verify it

```
pip install cryptography
python3 verify_pack.py <this-file>.zip --expect-fingerprint {manifest['signing']['key_fingerprint']}
```

Get the fingerprint from the issuer through a separate channel (not from this pack) before
relying on it. The verifier makes no network calls. It checks that:

1. `manifest.json` is signed by the enclosed Ed25519 key, and that key has the stated fingerprint;
2. every file listed in the manifest is present and matches its SHA-256, and nothing else was added;
3. `report.json` describes the same run state (results digest, configuration snapshot) as the manifest;
4. each included audit event still hashes to its recorded value.

## What this does and does not show

A valid pack shows these files are exactly what the issuer's ModelAssure workspace produced and
signed, unaltered since. It does not show that the tests were well chosen or that they ran
against the system you use: the issuer controls its own workspace. Read the report's methodology,
coverage and limitations sections, and re-run the suite yourself where your contract allows.

A PASS assessment means the defined tests passed under the stated configuration. It is not a
statement that the system is free of risk, not a regulatory certification, and not a release
authorisation. The regulatory mapping is not a legal or compliance opinion.

`run_configuration.json` has secret references removed and network addresses replaced by their
SHA-256 (listed under `redactions` in the manifest), so its hash differs from the run's
`snapshot_hash`, which covers the unredacted configuration.
""".encode()


def build_pack(db: Session, run: TestRun, p: Principal, case_evidence: CaseEvidence = "none") -> tuple[Artifact, dict[str, Any]]:
    from modules.reports.router import generate, outcomes_csv  # local import: avoids a cycle

    if run.status != "COMPLETED":
        raise conflict("evidence packs are created for completed runs")
    org = db.get(Organization, run.organization_id)
    project = db.get(Project, run.project_id)
    key = get_or_create_key(db, run.organization_id, p.user_id)

    j, pdf, report = generate(db, run, p)
    storage = get_storage()
    findings = list(db.scalars(select(Finding).where(Finding.run_id == run.id).order_by(Finding.finding_number)))
    reviews = list(db.scalars(select(Review).where(Review.run_id == run.id).order_by(Review.created_at)))
    config, redactions = redacted_configuration(run.config_snapshot)
    cases, case_meta = _case_evidence(db, run, case_evidence)

    files: dict[str, bytes] = {
        "report.json": storage.get(j.storage_ref),
        "report.pdf": storage.get(pdf.storage_ref),
        "outcomes.csv": outcomes_csv(db, run).encode(),
        "run_configuration.json": _json(config),
        "findings.json": _json([row(f) for f in findings]),
        "reviews.json": _json([row(r) for r in reviews]),
        "regulatory_mapping.json": _json(regulatory_mapping(db, run)),
        "audit_events.json": _json(_audit_subset(db, run, [str(f.id) for f in findings])),
        "verify_pack.py": VERIFIER.read_bytes(),
        "public_key.pem": key.public_key_pem.encode(),
        **cases,
    }
    for name, a in (("report.json", j), ("report.pdf", pdf)):
        if sha256_hex(files[name]) != a.checksum:
            raise conflict(f"stored {name} failed its integrity check")

    bv, cv = run.config_snapshot["connections"]["baseline"], run.config_snapshot["connections"]["candidate"]
    head = db.execute(select(AuditEvent.seq, AuditEvent.hash).where(AuditEvent.organization_id == run.organization_id)
                      .order_by(AuditEvent.seq.desc()).limit(1)).first()
    manifest: dict[str, Any] = {
        "format": FORMAT,
        "pack_id": str(uuid.uuid4()),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": p.email,
        "issuer": {"organization": org.name if org else None, "organization_id": str(run.organization_id)},
        "run": {"id": str(run.id), "run_number": run.run_number, "scope": run.scope, "project": project.name if project else None,
                "baseline_version": bv["version"], "candidate_version": cv["version"],
                "snapshot_hash": run.snapshot_hash, "results_digest": report["results_digest"],
                "assessment": report["executive_summary"]["assessment"], "report_version": j.version},
        "case_evidence": case_meta,
        "redactions": redactions,
        "audit_chain_head_at_signing": {"seq": head.seq, "hash": head.hash} if head else None,
        "files": [{"path": n, "sha256": sha256_hex(d), "bytes": len(d)} for n, d in sorted(files.items())],
        "signing": {"algorithm": "Ed25519", "key_fingerprint": key.fingerprint},
        "statement": ("Integrity and origin evidence only. Not a regulatory certification, legal opinion or release "
                      "authorisation; the receiving institution retains its own judgement and release authority."),
    }
    files["VERIFY.md"] = _verify_md(manifest)
    manifest["files"] = [{"path": n, "sha256": sha256_hex(d), "bytes": len(d)} for n, d in sorted(files.items())]
    manifest_bytes = (canonical_json(manifest) + "\n").encode()
    signature = sign(key, manifest_bytes)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", manifest_bytes)
        z.writestr("manifest.sig", signature + "\n")
        for n, d in sorted(files.items()):
            z.writestr(n, d)
    data = buf.getvalue()

    version = (db.scalar(select(func.max(Artifact.version)).where(Artifact.run_id == run.id, Artifact.type == "EVIDENCE_PACK")) or 0) + 1
    ref = storage.put(f"{run.organization_id}/runs/{run.id}/evidence-packs/v{version}.zip", data)
    a = Artifact(organization_id=run.organization_id, run_id=run.id, type="EVIDENCE_PACK", version=version, storage_ref=ref,
                 checksum=sha256_hex(data), size_bytes=len(data), created_by=p.user_id,
                 metadata_={"pack_id": manifest["pack_id"], "manifest_sha256": sha256_hex(manifest_bytes),
                            "key_fingerprint": key.fingerprint, "report_version": j.version, "case_evidence": case_meta,
                            "assessment": manifest["run"]["assessment"]})
    db.add(a)
    db.flush()
    return a, manifest
