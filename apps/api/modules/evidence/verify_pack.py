#!/usr/bin/env python3
"""Verify a ModelAssure evidence pack offline.

    python3 verify_pack.py evidence-pack.zip [--expect-fingerprint <sha256 hex>]

Needs only Python 3.9+ and the `cryptography` package (pip install cryptography). It makes no
network calls. Exit code 0 means every check passed; 1 means at least one failed; 2 means the
file could not be read as a pack.

What a valid pack proves: the files are exactly those the issuing ModelAssure workspace signed,
unaltered since signing, and signed by the key whose fingerprint is printed. Compare that
fingerprint with one the issuer gave you through a separate channel (or pass it with
--expect-fingerprint); a pack re-signed with someone else's key verifies against *their* key.

What it does not prove: that the tests were well designed or run against the system you use.
The issuer controls its own workspace. Read the report's methodology, coverage and limitations,
and re-run the suite yourself where your contract allows.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import zipfile
from typing import Any

FORMAT = "modelassure-evidence-pack/1"
SIGNATURE_FILES = {"manifest.json", "manifest.sig"}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def verify(path: str, expect_fingerprint: str | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(ok), "detail": detail})

    try:
        z = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as e:
        return {"valid": False, "readable": False, "checks": [{"check": "open", "passed": False, "detail": str(e)}]}
    with z:
        names = set(z.namelist())
        if not {"manifest.json", "manifest.sig", "public_key.pem"} <= names:
            return {"valid": False, "readable": False,
                    "checks": [{"check": "structure", "passed": False, "detail": "manifest.json, manifest.sig or public_key.pem missing"}]}
        manifest_bytes = z.read("manifest.json")
        try:
            manifest = json.loads(manifest_bytes)
        except ValueError:
            return {"valid": False, "readable": False, "checks": [{"check": "manifest", "passed": False, "detail": "manifest.json is not JSON"}]}
        check("format", manifest.get("format") == FORMAT, f"format {manifest.get('format')!r}")

        # 1. Signature over the exact manifest bytes
        pem = z.read("public_key.pem")
        fingerprint = None
        try:
            from cryptography.exceptions import InvalidSignature
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

            key = serialization.load_pem_public_key(pem)
            if not isinstance(key, Ed25519PublicKey):
                raise ValueError("public key is not Ed25519")
            raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
            fingerprint = _sha(raw)
            try:
                key.verify(base64.b64decode(z.read("manifest.sig")), manifest_bytes)
                check("signature", True, "manifest.json is signed by the enclosed Ed25519 key")
            except (InvalidSignature, ValueError):
                check("signature", False, "signature does not match manifest.json: the manifest was altered or signed by another key")
        except ImportError:
            check("signature", False, "the 'cryptography' package is required: pip install cryptography")
        except ValueError as e:
            check("signature", False, f"unusable public key: {e}")

        signing = manifest.get("signing", {})
        check("key_fingerprint", fingerprint is not None and fingerprint == signing.get("key_fingerprint"),
              f"enclosed key fingerprint {fingerprint} (manifest says {signing.get('key_fingerprint')})")
        if expect_fingerprint:
            check("expected_signer", fingerprint == expect_fingerprint.strip().lower(),
                  "matches the fingerprint you supplied" if fingerprint == expect_fingerprint.strip().lower()
                  else "does NOT match the fingerprint you supplied")

        # 2. Every listed file present and unaltered; nothing unlisted
        listed = {f["path"]: f for f in manifest.get("files", [])}
        bad = []
        for p, f in sorted(listed.items()):
            if p not in names:
                bad.append(f"{p}: missing")
                continue
            data = z.read(p)
            if _sha(data) != f.get("sha256") or len(data) != f.get("bytes"):
                bad.append(f"{p}: content differs from the signed hash")
        check("file_hashes", not bad, f"{len(listed)} files match their signed SHA-256" if not bad else "; ".join(bad[:10]))
        extra = sorted(names - set(listed) - SIGNATURE_FILES)
        check("no_unlisted_files", not extra, "no files outside the manifest" if not extra else f"unlisted files: {', '.join(extra[:10])}")

        # 3. Report is the one the manifest describes
        if "report.json" in names:
            try:
                rep = json.loads(z.read("report.json"))
                run = manifest.get("run", {})
                ok = (rep.get("results_digest") == run.get("results_digest")
                      and rep.get("run", {}).get("snapshot_hash", run.get("snapshot_hash")) == run.get("snapshot_hash"))
                check("report_matches_manifest", ok, "report results digest and configuration snapshot match the manifest"
                      if ok else "report.json describes a different run state than the manifest")
            except ValueError:
                check("report_matches_manifest", False, "report.json is not JSON")

        # 4. Audit events: each event's hash recomputes from its content and predecessor
        if "audit_events.json" in names:
            try:
                events = json.loads(z.read("audit_events.json"))
                broken = [e["payload"]["seq"] for e in events
                          if _sha(e["prev_hash"] + _sha(_canonical(e["payload"]))) != e["hash"]]
                check("audit_event_hashes", not broken, f"{len(events)} audit events recompute to their recorded hashes"
                      if not broken else f"events with altered content: {broken[:10]}")
            except (ValueError, KeyError, TypeError):
                check("audit_event_hashes", False, "audit_events.json is malformed")

    return {"valid": all(c["passed"] for c in checks), "readable": True, "key_fingerprint": fingerprint,
            "issuer": manifest.get("issuer"), "run": manifest.get("run"), "created_at": manifest.get("created_at"),
            "checks": checks}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Verify a ModelAssure evidence pack offline.")
    ap.add_argument("pack")
    ap.add_argument("--expect-fingerprint", help="SHA-256 fingerprint of the issuer's public key, obtained separately")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    a = ap.parse_args(argv)
    r = verify(a.pack, a.expect_fingerprint)
    if a.json:
        print(json.dumps(r, indent=2))
    else:
        for c in r["checks"]:
            print(f"[{'PASS' if c['passed'] else 'FAIL'}] {c['check']}: {c['detail']}")
        if r.get("readable"):
            run = r.get("run") or {}
            print(f"\nIssuer: {(r.get('issuer') or {}).get('organization')}  ·  key {r.get('key_fingerprint')}")
            print(f"Run #{run.get('run_number')} {run.get('baseline_version')} → {run.get('candidate_version')}"
                  f"  ·  assessment {run.get('assessment')}  ·  signed {r.get('created_at')}")
        print("\nVALID" if r["valid"] else "\nNOT VALID")
        if r["valid"] and not a.expect_fingerprint:
            print("Compare the key fingerprint above with the one the issuer gave you separately.")
    return 0 if r["valid"] else (2 if not r.get("readable") else 1)


if __name__ == "__main__":
    sys.exit(main())
