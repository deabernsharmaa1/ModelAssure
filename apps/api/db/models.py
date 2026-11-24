"""PostgreSQL system of record (spec §9).

Every organisation-owned table carries `organization_id` and is protected by row-level security
(see infrastructure/migrations). UUID primary keys throughout. Records that form evidence
(system versions, approved policy sets, suites, executions, audit events, artifacts) are
immutable: changes create new versioned rows, enforced by database triggers.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def org_fk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)


def created() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def user_fk(nullable: bool = True) -> Mapped[uuid.UUID | None]:
    return mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=nullable)


# ---------------------------------------------------------------------------------------------
class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="ACTIVE")
    approved_destinations: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = created()


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    identity_provider_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))  # dev auth only
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = created()


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id", "role"),)
    id: Mapped[uuid.UUID] = pk()
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    organization_id: Mapped[uuid.UUID] = org_fk()
    role: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = created()


class System(Base):
    __tablename__ = "systems"
    __table_args__ = (UniqueConstraint("organization_id", "name"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. SME_LENDING_DECISION
    vendor: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[uuid.UUID | None] = user_fk()
    created_at: Mapped[datetime] = created()


class SystemVersion(Base):
    __tablename__ = "system_versions"
    __table_args__ = (UniqueConstraint("system_id", "version"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    system_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("systems.id"), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    expected_model_version: Mapped[str | None] = mapped_column(String(100))
    artifact_digest: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    registered_by: Mapped[uuid.UUID | None] = user_fk()
    created_at: Mapped[datetime] = created()


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    business_use_case: Mapped[str] = mapped_column(Text, nullable=False)
    system_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("systems.id"))
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    baseline_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("system_versions.id"))
    candidate_version_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("system_versions.id"))
    risk_classification: Mapped[str] = mapped_column(String(20), nullable=False)  # HIGH | MEDIUM | LOW
    test_environment: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="ACTIVE")
    created_by: Mapped[uuid.UUID | None] = user_fk()
    created_at: Mapped[datetime] = created()


class Connection(Base):
    __tablename__ = "connections"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    system_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("system_versions.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # REST | RECORDED
    endpoint: Mapped[str | None] = mapped_column(Text)
    environment: Mapped[str] = mapped_column(String(20), nullable=False, server_default="SANDBOX")
    auth_header: Mapped[str | None] = mapped_column(String(100))
    secret_ref: Mapped[str | None] = mapped_column(String(200))
    mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    mapping_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5000")
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    max_rps: Mapped[float | None] = mapped_column(Float)
    dependency_control_url: Mapped[str | None] = mapped_column(Text)
    dependency_secret_ref: Mapped[str | None] = mapped_column(String(200))
    recorded_dataset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("datasets.id"))
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="UNVERIFIED")
    verification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = user_fk()
    created_at: Mapped[datetime] = created()


class Dataset(Base):
    """Customer-authorised historical cases or recorded system outputs (JSON/CSV integration mode)."""
    __tablename__ = "datasets"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # HISTORICAL_CASES | RECORDED_OUTPUTS
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[uuid.UUID | None] = user_fk()
    created_at: Mapped[datetime] = created()


class PolicySet(Base):
    __tablename__ = "policy_sets"
    __table_args__ = (UniqueConstraint("project_id", "name", "version"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("policy_sets.id"))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    approval_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="DRAFT")
    created_by: Mapped[uuid.UUID | None] = user_fk()
    submitted_by: Mapped[uuid.UUID | None] = user_fk()
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[uuid.UUID | None] = user_fk()
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()


class PolicyRow(Base):
    __tablename__ = "policies"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    policy_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("policy_sets.id"), nullable=False, index=True)
    policy_id: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)  # ASSERTION | CHANGE_CONTROL | EXPECTED_CHANGE
    expression: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    severity: Mapped[str | None] = mapped_column(String(20))
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class TestSuite(Base):
    __test__ = False  # not a pytest class
    __tablename__ = "test_suites"
    __table_args__ = (UniqueConstraint("project_id", "name", "version"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    generator_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    category_counts: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_by: Mapped[uuid.UUID | None] = user_fk()
    created_at: Mapped[datetime] = created()


class TestCase(Base):
    __test__ = False  # not a pytest class
    __tablename__ = "test_cases"
    __table_args__ = (UniqueConstraint("suite_id", "case_key"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    suite_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("test_suites.id"), nullable=False, index=True)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    case_key: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    input: Mapped[Any] = mapped_column(JSONB, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scenario: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    expected_behavior: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    tags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    skip_reason: Mapped[str | None] = mapped_column(Text)


class TestRun(Base):
    __test__ = False  # not a pytest class
    __tablename__ = "test_runs"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False, index=True)
    run_number: Mapped[int] = mapped_column(Integer, nullable=False)
    suite_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("test_suites.id"), nullable=False)
    baseline_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("system_versions.id"), nullable=False)
    candidate_version_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("system_versions.id"), nullable=False)
    policy_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("policy_sets.id"), nullable=False)
    baseline_policy_set_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("policy_sets.id"), nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False, server_default="SANDBOX")
    scope: Mapped[str] = mapped_column(String(20), nullable=False, server_default="FULL")  # FULL | RETEST
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("test_runs.id"))
    case_filter: Mapped[list[str] | None] = mapped_column(JSONB)  # case ids for RETEST scope
    retest_finding_ids: Mapped[list[str] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="QUEUED")
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    progress: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    summary: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    assessment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[uuid.UUID | None] = user_fk()
    created_at: Mapped[datetime] = created()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TestExecution(Base):
    __test__ = False  # not a pytest class
    __tablename__ = "test_executions"
    __table_args__ = (UniqueConstraint("run_id", "case_id"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("test_runs.id"), nullable=False, index=True)
    case_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("test_cases.id"), nullable=False)
    case_key: Mapped[str] = mapped_column(String(100), nullable=False)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    result: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    baseline_decision: Mapped[str | None] = mapped_column(String(20))
    candidate_decision: Mapped[str | None] = mapped_column(String(20))
    changed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    change_class: Mapped[str | None] = mapped_column(String(20))
    failure_types: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    violated_rules: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    error_type: Mapped[str | None] = mapped_column(String(40))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # evaluation outcome (small)
    evidence_ref: Mapped[str] = mapped_column(Text, nullable=False)  # raw requests/responses in object storage
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    observed_versions: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    duration_ms: Mapped[float | None] = mapped_column(Float)
    executed_at: Mapped[datetime] = created()


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (UniqueConstraint("organization_id", "finding_number"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    finding_number: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("test_runs.id"), nullable=False, index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("projects.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(40), nullable=False)
    signature: Mapped[str] = mapped_column(String(300), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    rule_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, server_default="[]")
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    suggested_severity: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, server_default="OPEN")
    owner_id: Mapped[uuid.UUID | None] = user_fk()
    affected_count: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    suspected_cause: Mapped[str | None] = mapped_column(Text)
    risk_acceptance: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    closure: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    retest_history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, server_default="[]")
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = created()


class FindingExecution(Base):
    __tablename__ = "finding_executions"
    finding_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("findings.id"), primary_key=True)
    execution_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("test_executions.id"), primary_key=True)
    organization_id: Mapped[uuid.UUID] = org_fk()


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("test_runs.id"), nullable=False, index=True)
    reviewer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    assessment_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created()


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("test_runs.id"), index=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False)  # REPORT_JSON | REPORT_PDF | REPORT_CSV | DATASET
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_ref: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, nullable=False, server_default="{}")
    created_by: Mapped[uuid.UUID | None] = user_fk()
    created_at: Mapped[datetime] = created()


class SigningKey(Base):
    """Per-organisation Ed25519 key used to sign evidence packs. The private key lives in the
    secret store; only its reference is kept here. Retired keys stay listed so older packs remain
    attributable."""
    __tablename__ = "signing_keys"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    algorithm: Mapped[str] = mapped_column(String(20), nullable=False, server_default="Ed25519")
    public_key_pem: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    secret_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="ACTIVE")  # ACTIVE | RETIRED
    created_by: Mapped[uuid.UUID | None] = user_fk()
    created_at: Mapped[datetime] = created()
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (UniqueConstraint("organization_id", "seq"),)
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = org_fk()
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = user_fk()
    actor_label: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(100))
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, server_default="SUCCESS")
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)


class Job(Base):
    """PostgreSQL-backed work queue (replaces Celery + Redis; claimed with FOR UPDATE SKIP LOCKED)."""
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = pk()
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="QUEUED", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="3")
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = created()
    updated_at: Mapped[datetime] = created()


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    organization_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("organizations.id"), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    endpoint: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created()


TENANT_TABLES = [
    "systems", "system_versions", "projects", "connections", "datasets", "policy_sets", "policies",
    "test_suites", "test_cases", "test_runs", "test_executions", "findings", "finding_executions",
    "reviews", "artifacts", "audit_events", "idempotency_keys", "signing_keys",
]
