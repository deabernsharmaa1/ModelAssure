"""Report generation and export (Module G): PDF, machine-readable JSON, CSV of permitted outcomes."""
from __future__ import annotations

import csv
import io
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session

from common.deps import get_or_404, row, tenant_db
from common.errors import conflict, not_found
from common.hashing import canonical_json, sha256_hex
from common.storage import get_storage
from db.models import Artifact, TestExecution, TestRun
from modules.audit.service import record_for
from modules.identity.auth import Principal, require
from modules.reports.builder import build_report, next_report_version

router = APIRouter(prefix="/v1", tags=["reports"])
_env = Environment(loader=FileSystemLoader(str(Path(__file__).parent / "templates")),
                   autoescape=select_autoescape(["html"]))


def _counts(d) -> str:
    """{'APPROVE': 27, 'REFER': 6} → 'APPROVE ×27 · REFER ×6'"""
    if not isinstance(d, dict) or not d:
        return "—"
    return " · ".join(f"{k} ×{v}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]))


_env.filters["counts"] = _counts


def render_pdf(report: dict) -> bytes:
    from weasyprint import HTML

    return HTML(string=_env.get_template("report.html").render(r=report)).write_pdf()


def _store(db: Session, run: TestRun, p: Principal, kind: str, version: int, data: bytes, ext: str, meta: dict) -> Artifact:
    key = f"{run.organization_id}/runs/{run.id}/reports/v{version}.{ext}"
    ref = get_storage().put(key, data)
    a = Artifact(organization_id=run.organization_id, run_id=run.id, type=kind, version=version, storage_ref=ref,
                 checksum=sha256_hex(data), size_bytes=len(data), metadata_=meta, created_by=p.user_id)
    db.add(a)
    db.flush()
    return a


def generate(db: Session, run: TestRun, p: Principal) -> tuple[Artifact, Artifact, dict]:
    if run.status != "COMPLETED":
        raise conflict("reports are generated for completed runs")
    version = next_report_version(db, run)
    report = build_report(db, run, version, p.email)
    data = canonical_json(report).encode()
    meta = {"results_digest": report["results_digest"], "assessment": report["executive_summary"]["assessment"],
            "snapshot_hash": run.snapshot_hash}
    j = _store(db, run, p, "REPORT_JSON", version, data, "json", meta)
    pdf = _store(db, run, p, "REPORT_PDF", version, render_pdf(report), "pdf", {**meta, "json_checksum": j.checksum})
    record_for(db, p, "report.generate", "test_run", run.id, {"version": version, **meta})
    return j, pdf, report


@router.post("/runs/{run_id}/report", status_code=201)
def create_report(run_id: str, p: Principal = Depends(require("report.read")), db: Session = Depends(tenant_db)):
    run = get_or_404(db, TestRun, run_id, "run")
    j, pdf, report = generate(db, run, p)
    db.commit()
    return {"version": j.version, "json": row(j), "pdf": row(pdf), "results_digest": report["results_digest"]}


@router.get("/runs/{run_id}/reports")
def list_reports(run_id: str, p: Principal = Depends(require("report.read")), db: Session = Depends(tenant_db)):
    run = get_or_404(db, TestRun, run_id, "run")
    return [row(a) for a in db.scalars(select(Artifact).where(Artifact.run_id == run.id, Artifact.type.like("REPORT_%"))
                                       .order_by(Artifact.version, Artifact.type))]


@router.get("/runs/{run_id}/report")
def get_report(run_id: str, format: str = Query("json", pattern="^(json|pdf)$"), version: int | None = None,
               p: Principal = Depends(require("report.read")), db: Session = Depends(tenant_db)):
    """Retrieve a stored report version (latest by default; generated on first request).
    Integrity is verified against the stored checksum on every download."""
    run = get_or_404(db, TestRun, run_id, "run")
    kind = "REPORT_PDF" if format == "pdf" else "REPORT_JSON"
    q = select(Artifact).where(Artifact.run_id == run.id, Artifact.type == kind)
    q = q.where(Artifact.version == version) if version else q.order_by(Artifact.version.desc())
    a = db.scalars(q.limit(1)).first()
    if a is None:
        if version:
            raise not_found("report version")
        j, pdf, _ = generate(db, run, p)
        a = pdf if format == "pdf" else j
    data = get_storage().get(a.storage_ref)
    if sha256_hex(data) != a.checksum:
        raise conflict("stored report failed its integrity check")
    record_for(db, p, "report.download", "test_run", run.id, {"version": a.version, "format": format})
    db.commit()
    headers = {"X-Report-Version": str(a.version), "X-Content-SHA256": a.checksum,
               "Content-Disposition": f'attachment; filename="modelassure-run-{run.run_number}-v{a.version}.{format}"'}
    if format == "pdf":
        return Response(data, media_type="application/pdf", headers=headers)
    return Response(data, media_type="application/json", headers=headers)


CSV_FIELDS = ["case_key", "category", "result", "baseline_decision", "candidate_decision", "changed", "change_class",
              "violated_rules", "failure_types", "error_type", "trace_id", "evidence_hash"]


def outcomes_csv(db: Session, run: TestRun) -> str:
    """Permitted outcomes only: no applicant inputs or raw responses."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    w.writeheader()
    for e in db.scalars(select(TestExecution).where(TestExecution.run_id == run.id).order_by(TestExecution.case_key)):
        w.writerow({"case_key": e.case_key, "category": e.category, "result": e.result,
                    "baseline_decision": e.baseline_decision, "candidate_decision": e.candidate_decision,
                    "changed": e.changed, "change_class": e.change_class or "", "violated_rules": ";".join(e.violated_rules),
                    "failure_types": ";".join(e.failure_types), "error_type": e.error_type or "", "trace_id": e.trace_id or "",
                    "evidence_hash": e.evidence_hash})
    return buf.getvalue()


@router.get("/runs/{run_id}/executions.csv")
def export_csv(run_id: str, p: Principal = Depends(require("report.read")), db: Session = Depends(tenant_db)):
    """Permitted outcomes only: no applicant inputs or raw responses leave via CSV."""
    run = get_or_404(db, TestRun, run_id, "run")
    data = outcomes_csv(db, run)
    record_for(db, p, "report.export_csv", "test_run", run.id)
    db.commit()
    return Response(data, media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="modelassure-run-{run.run_number}-outcomes.csv"'})
