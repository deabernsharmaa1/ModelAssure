"""Datasets (historical replay / recorded outputs) and versioned test suites (Module D)."""
from __future__ import annotations

import csv
import io
import uuid
from collections import Counter
from typing import Any, Literal

import pandas as pd
import pandera.pandas as pa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from common.deps import get_or_404, row, tenant_db
from common.errors import invalid
from common.hashing import content_hash
from common.storage import get_json, put_json
from db.models import Dataset, PolicySet, Project, TestCase, TestSuite
from modules.audit.service import record_for
from modules.identity.auth import Principal, require
from modules.policies.dsl import PolicyError
from modules.policies.policy_set import compile_policy_set
from modules.scenarios.generators import GenerationError, default_scenario, generate_suite

router = APIRouter(prefix="/v1", tags=["scenarios"])

MAX_DATASET_ROWS = 50_000

# Pandera contract for historical case CSVs (spec §5.1 Data validation)
HISTORICAL_SCHEMA = pa.DataFrameSchema(
    {
        "case_id": pa.Column(str, unique=True, nullable=False, checks=pa.Check.str_length(1, 64)),
        "product": pa.Column(str, nullable=False),
        "requested_amount": pa.Column(float, pa.Check.gt(0), nullable=False, coerce=True),
        "currency": pa.Column(str, nullable=False),
        "monthly_cash_available": pa.Column(float, pa.Check.ge(0), nullable=True, coerce=True, required=False),
        "monthly_debt_service": pa.Column(float, pa.Check.ge(0), nullable=True, coerce=True, required=False),
        "bureau_status": pa.Column(str, pa.Check.isin(["AVAILABLE", "UNAVAILABLE"]), nullable=True, required=False),
        "bureau_score": pa.Column(float, pa.Check.in_range(300, 900), nullable=True, coerce=True, required=False),
        "fraud_status": pa.Column(str, pa.Check.isin(["CLEAR", "MEDIUM", "HIGH"]), nullable=True, required=False),
        "fraud_alert_resolved": pa.Column(bool, nullable=True, coerce=True, required=False),
        "identity_status": pa.Column(str, pa.Check.isin(["VERIFIED", "PENDING", "FAILED"]), nullable=True, required=False),
    },
    strict=True,
)


def _csv_to_cases(text_csv: str) -> list[dict[str, Any]]:
    df = pd.read_csv(io.StringIO(text_csv), dtype={"case_id": str})
    try:
        df = HISTORICAL_SCHEMA.validate(df, lazy=True)
    except pa.errors.SchemaErrors as e:
        fc = e.failure_cases.head(20)
        raise invalid("historical dataset failed validation",
                      [{"column": str(r.column), "check": str(r.check), "value": str(r.failure_case), "row": None if pd.isna(r.index) else int(r.index)}
                       for r in fc.itertuples()]) from None
    out = []
    for rec in df.to_dict(orient="records"):
        case: dict[str, Any] = {}
        for k, v in rec.items():
            if v is None or (isinstance(v, float) and pd.isna(v)):
                continue
            if k.startswith(("bureau_", "fraud_", "identity_")):
                grp, field = k.split("_", 1)
                if grp == "bureau" and field == "score":
                    v = int(v)
                case.setdefault(grp, {})[field] = v.item() if hasattr(v, "item") else v
            else:
                case[k] = v.item() if hasattr(v, "item") else v
        out.append(case)
    return out


class DatasetIn(BaseModel):
    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["HISTORICAL_CASES", "RECORDED_OUTPUTS"]
    rows: list[dict[str, Any]] | None = Field(default=None, description="JSON rows")
    csv: str | None = Field(default=None, description="CSV text (HISTORICAL_CASES only)")


@router.post("/datasets", status_code=201)
def create_dataset(body: DatasetIn, p: Principal = Depends(require("dataset.manage")), db: Session = Depends(tenant_db)):
    """Customer-authorised data only. Version 1 default is synthetic data; real data requires a
    documented data-processing agreement (spec §12.3)."""
    get_or_404(db, Project, body.project_id, "project")
    if (body.rows is None) == (body.csv is None):
        raise invalid("provide exactly one of rows or csv")
    if body.csv is not None:
        if body.kind != "HISTORICAL_CASES":
            raise invalid("CSV upload is supported for HISTORICAL_CASES")
        rows = _csv_to_cases(body.csv)
    else:
        rows = body.rows or []
    if not rows or len(rows) > MAX_DATASET_ROWS:
        raise invalid(f"dataset must have 1..{MAX_DATASET_ROWS} rows")
    if body.kind == "HISTORICAL_CASES":
        ids = [r.get("case_id") for r in rows]
        if any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids):
            raise invalid("every historical case needs a unique non-empty case_id")
    else:
        for r in rows:
            if not isinstance(r.get("case_id"), str) or not isinstance(r.get("response"), dict):
                raise invalid("each recorded output row needs case_id and a response object")
        if len({r["case_id"] for r in rows}) != len(rows):
            raise invalid("duplicate case_id in recorded outputs")
    ds_id = uuid.uuid4()
    ref, digest, _ = put_json(f"{p.org_id}/datasets/{ds_id}.json", rows)
    ds = Dataset(id=ds_id, organization_id=p.org_id, project_id=body.project_id, name=body.name, kind=body.kind,
                 storage_ref=ref, checksum=digest, row_count=len(rows), created_by=p.user_id)
    db.add(ds)
    db.flush()
    record_for(db, p, "dataset.create", "dataset", ds.id, {"kind": ds.kind, "rows": ds.row_count, "checksum": digest})
    db.commit()
    return row(ds)


@router.get("/datasets")
def list_datasets(project_id: uuid.UUID | None = None, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    q = select(Dataset).order_by(Dataset.created_at.desc())
    if project_id:
        q = q.where(Dataset.project_id == project_id)
    return [row(d) for d in db.scalars(q)]


def load_dataset_rows(ds: Dataset) -> list[dict[str, Any]]:
    from common.hashing import canonical_json, sha256_hex

    rows = get_json(ds.storage_ref)
    if sha256_hex(canonical_json(rows)) != ds.checksum:
        raise invalid(f"dataset {ds.id} failed its integrity check")
    return rows


# ---- test suites -----------------------------------------------------------------------------
class SuiteIn(BaseModel):
    project_id: uuid.UUID
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    config: dict[str, Any] = Field(description="Generator configuration, see test_suites/lending/*.json")
    policy_set_ids: list[uuid.UUID] = Field(default_factory=list, description="Policy sets whose thresholds drive boundary tests")
    historical_dataset_id: uuid.UUID | None = None


def _compile_for_boundaries(db: Session, ids: list[uuid.UUID]):
    out = []
    for i in ids:
        ps = get_or_404(db, PolicySet, i, "policy set")
        try:
            out.append(compile_policy_set(ps.content))
        except PolicyError as e:
            raise invalid(f"policy set {i} is invalid: {e}") from None
    return out


@router.post("/test-suites/preview")
def preview_suite(body: SuiteIn, p: Principal = Depends(require("suite.create")), db: Session = Depends(tenant_db)):
    cps = _compile_for_boundaries(db, body.policy_set_ids)
    try:
        cases = generate_suite(body.config, cps, [] if not body.historical_dataset_id else
                               load_dataset_rows(get_or_404(db, Dataset, body.historical_dataset_id, "dataset")))
    except GenerationError as e:
        raise invalid(str(e)) from None
    return {"case_count": len(cases), "categories": dict(Counter(c["category"] for c in cases)),
            "sample": cases[:5]}


@router.post("/test-suites", status_code=201)
def create_suite(body: SuiteIn, p: Principal = Depends(require("suite.create")), db: Session = Depends(tenant_db)):
    get_or_404(db, Project, body.project_id, "project")
    cps = _compile_for_boundaries(db, body.policy_set_ids)
    hist: list[dict] = []
    if body.historical_dataset_id:
        ds = get_or_404(db, Dataset, body.historical_dataset_id, "dataset")
        if ds.kind != "HISTORICAL_CASES":
            raise invalid("historical_dataset_id must reference a HISTORICAL_CASES dataset")
        hist = load_dataset_rows(ds)
        if not any(g.get("type") == "historical" for g in body.config.get("generators", [])):
            body.config = {**body.config, "generators": [*body.config.get("generators", []), {"type": "historical"}]}
    try:
        cases = generate_suite(body.config, cps, hist)
    except GenerationError as e:
        raise invalid(str(e)) from None
    if not cases:
        raise invalid("the configuration produced no test cases")
    db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"suite:{body.project_id}:{body.name}"})
    version = (db.scalar(select(func.max(TestSuite.version)).where(TestSuite.project_id == body.project_id,
                                                                   TestSuite.name == body.name)) or 0) + 1
    config = {**body.config, "policy_set_ids": [str(i) for i in body.policy_set_ids],
              "historical_dataset_id": str(body.historical_dataset_id) if body.historical_dataset_id else None}
    suite = TestSuite(organization_id=p.org_id, project_id=body.project_id, name=body.name, version=version,
                      description=body.description, generator_config=config, seed=int(body.config.get("seed", 20261124)),
                      content_hash=content_hash(cases), case_count=len(cases),
                      category_counts=dict(Counter(c["category"] for c in cases)), created_by=p.user_id)
    db.add(suite)
    db.flush()
    db.bulk_insert_mappings(TestCase, [{
        "id": uuid.uuid4(), "organization_id": p.org_id, "suite_id": suite.id, "seq": i, "case_key": c["case_key"],
        "category": c["category"], "input": c["input"], "input_hash": content_hash(c["input"]),
        "scenario": c["scenario"] or default_scenario(), "expected_behavior": c["expected_behavior"] or {},
        "tags": c["tags"],
    } for i, c in enumerate(cases)])
    record_for(db, p, "test_suite.create", "test_suite", suite.id,
               {"name": suite.name, "version": version, "cases": len(cases), "content_hash": suite.content_hash})
    db.commit()
    return row(suite)


@router.get("/test-suites")
def list_suites(project_id: uuid.UUID | None = None, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    q = select(TestSuite).order_by(TestSuite.created_at.desc())
    if project_id:
        q = q.where(TestSuite.project_id == project_id)
    return [row(s) for s in db.scalars(q)]


@router.get("/test-suites/{suite_id}")
def get_suite(suite_id: str, p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    return row(get_or_404(db, TestSuite, suite_id, "test suite"))


@router.get("/test-suites/{suite_id}/cases")
def list_cases(suite_id: str, category: str | None = None, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
               p: Principal = Depends(require("project.read")), db: Session = Depends(tenant_db)):
    get_or_404(db, TestSuite, suite_id, "test suite")
    q = select(TestCase).where(TestCase.suite_id == uuid.UUID(suite_id))
    if category:
        q = q.where(TestCase.category == category)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    return {"total": total, "items": [row(c) for c in db.scalars(q.order_by(TestCase.seq).limit(limit).offset(offset))]}


def csv_rows(rows: list[dict[str, Any]], fields: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    return buf.getvalue()
