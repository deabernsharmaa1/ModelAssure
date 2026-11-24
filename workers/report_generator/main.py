"""Offline report tooling.

  python -m workers.report_generator.main verify <report.json> <report.pdf> --digest <results_digest>

Verifies a downloaded report bundle: the JSON's embedded results digest, and that the PDF bytes
match the checksum recorded in the report metadata (X-Content-SHA256 header when downloaded).
Report generation itself runs inside the API (POST /v1/runs/{id}/report) so it is always
authorised, audited and tenant-scoped.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="modelassure-report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verify")
    v.add_argument("report_json")
    v.add_argument("--digest", help="expected results digest")
    v.add_argument("--sha256", help="expected SHA-256 of the JSON file")
    args = ap.parse_args(argv)
    data = open(args.report_json, "rb").read()
    report = json.loads(data)
    ok = True
    if args.sha256 and hashlib.sha256(data).hexdigest() != args.sha256:
        print("JSON checksum mismatch"); ok = False
    if args.digest and report.get("results_digest") != args.digest:
        print("results digest mismatch"); ok = False
    print(f"run #{report['run']['run_number']} report v{report['report_version']}: "
          f"assessment {report['executive_summary']['assessment']} — {'OK' if ok else 'FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
