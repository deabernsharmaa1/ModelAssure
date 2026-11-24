export type Role = "ADMIN" | "RISK_MANAGER" | "ENGINEER" | "ANALYST" | "REVIEWER" | "AUDITOR";
export type Decision = "APPROVE" | "DECLINE" | "REVIEW" | "HOLD" | "REFER";
export type ResultState = "PASS" | "FAIL" | "CHANGED" | "ERROR" | "INCONCLUSIVE" | "SKIPPED";
export type Severity = "CRITICAL" | "HIGH" | "MEDIUM" | "LOW";
export type Outcome = "PASS" | "FAIL" | "INCONCLUSIVE";

export interface Me {
  id: string; email: string; name: string; roles: Role[];
  organization: { id: string; name: string }; mfa: boolean;
}
export interface UserRef { id: string; email: string; name: string; roles: Role[] }

export interface System { id: string; name: string; category: string; vendor: string | null }
export interface SystemVersion {
  id: string; system_id: string; version: string; expected_model_version: string | null;
  artifact_digest: string | null; description: string | null; checksum: string; created_at: string;
}

export interface Project {
  id: string; name: string; business_use_case: string; system_id: string; owner_id: string;
  baseline_version_id: string; candidate_version_id: string; risk_classification: "HIGH" | "MEDIUM" | "LOW";
  test_environment: string; status: string; created_at: string;
  open_findings?: number;
  latest_run?: { id: string; run_number: number; status: string; assessment: Outcome | null } | null;
  system?: System; baseline_version?: SystemVersion; candidate_version?: SystemVersion;
}

export interface VerificationCheck { check: string; passed: boolean; detail: string; level: string }
export interface Connection {
  id: string; system_version_id: string; kind: "REST" | "RECORDED"; endpoint: string | null; environment: string;
  auth_header: string | null; has_credential: boolean; mapping: Record<string, unknown>; mapping_hash: string;
  timeout_ms: number; max_concurrency: number; max_rps: number | null; dependency_control_url: string | null;
  status: "UNVERIFIED" | "VERIFIED" | "FAILED"; verification: { checks: VerificationCheck[]; verified_by: string } | null;
  verified_at: string | null; created_at: string;
}

export interface Issue { level: "error" | "warning"; code: string; message: string; refs: string[] }
export interface PolicySet {
  id: string; project_id: string; name: string; version: number; parent_id: string | null;
  content?: Record<string, unknown>; content_hash: string;
  validation: { valid: boolean; issues: Issue[]; counts?: Record<string, number> };
  approval_status: "DRAFT" | "PENDING_REVIEW" | "APPROVED" | "REJECTED";
  created_by: string | null; approved_by: string | null; approved_at: string | null; review_note: string | null; created_at: string;
}

export interface TestSuite {
  id: string; project_id: string; name: string; version: number; description: string | null;
  generator_config: Record<string, unknown>; seed: number; content_hash: string; case_count: number;
  category_counts: Record<string, number>; created_at: string;
}

export interface CoverageRow { category: string; total: number; pass: number; fail: number; changed: number; error: number; inconclusive: number; skipped: number }
export interface RunSummary {
  total: number; by_result: Record<ResultState, number>; coverage: CoverageRow[];
  decision_agreement_rate: number | null; agreement_note: string;
  transition_matrix: Record<string, Record<string, number>>; transitions: Record<string, number>;
  exposure: { newly_approved_amount: number; no_longer_approved_amount: number; note: string };
  changes: { total: number; expected: number; unexpected: number; prohibited: number; with_violation: number };
  violations_by_rule: Record<string, number>; failure_types: Record<string, number>;
  dependency_behaviour: { fault: string; cases: number; candidate_decisions: Record<string, number>; baseline_decisions: Record<string, number>; failed: number; errors: number }[];
  error_rate: number; timeout_rate: number; inconclusive_rate: number; errors_by_type: Record<string, number>;
  latency: Record<string, { p50_ms: number; p95_ms: number } | null>;
}
export interface Assessment {
  outcome: Outcome; recommendation: string; fail_reasons: string[]; inconclusive_reasons: string[]; flags: string[];
  accepted_risks: { finding: string; title: string; severity: Severity; owner: string; rationale: string; authorised_by: string }[];
  disclaimer: string; assessed_at: string;
}
export interface Run {
  id: string; project_id: string; run_number: number; suite_id: string; status: string; scope: "FULL" | "RETEST";
  parent_run_id: string | null; snapshot_hash: string; created_at: string; started_at: string | null; completed_at: string | null;
  progress: { completed: number; total: number; percent: number }; summary: RunSummary | null; assessment: Assessment | null;
  failure_reason: string | null; mode: string; review_count: number; last_review_decision: string | null;
  config: {
    suite: { name: string; version: number; case_count: number; content_hash: string };
    versions: { baseline: { version: string }; candidate: { version: string } };
    policy_sets: Record<"baseline" | "candidate", { name: string; version: number; label: string; approval_status: string }>;
    connections: Record<"baseline" | "candidate", { endpoint: string; kind: string; mapping_hash: string }>;
    exclusions: { categories?: string[]; reason?: string } | null;
  };
}

export interface Violation { policy_id: string | null; failure_type: string; expected: unknown; observed: unknown; severity: Severity; reason?: string }
export interface Execution {
  id: string; run_id: string; case_key: string; category: string; result: ResultState;
  baseline_decision: Decision | null; candidate_decision: Decision | null; changed: boolean; change_class: string | null;
  failure_types: string[]; violated_rules: string[]; error_type: string | null; trace_id: string | null;
  evidence_hash: string; duration_ms: number | null;
  summary?: { violations: Violation[]; inconclusive_reasons: string[]; explained_by: string[]; error: { type: string; detail: string } | null; change_flags: { rule_id: string; severity: Severity }[] };
}
export interface ObservationEvidence {
  status: string; http_status: number | null; error_type: string | null; error_detail: string | null;
  raw_request: unknown; raw_response: unknown; normalized: Record<string, unknown> | null;
  transformations: { field: string; source: string; raw: unknown; normalized: unknown }[]; duration_ms: number | null;
}
export interface ExecutionDetail extends Execution {
  detail: { policy_outcomes: { policy_id: string; status: string; expected: string; observed: unknown; reason: string; severity: Severity }[] } & Record<string, unknown>;
  evidence: null | { input: unknown; scenario: Record<string, unknown>; baseline: ObservationEvidence | null; candidate: ObservationEvidence | null; trace_id: string; executed_at: string; schema_versions: Record<string, string> };
  evidence_integrity: { sha256: string; verified: boolean; purged?: boolean };
}

export interface Finding {
  id: string; key: string; finding_number: number; run_id: string; run_number: number; project_id: string;
  type: string; signature: string; title: string; rule_ids: string[]; severity: Severity; suggested_severity: Severity;
  status: string; owner_id: string | null; owner?: { id: string; name: string; email: string } | null;
  affected_count: number; suspected_cause: string | null;
  summary: { affected_case_keys: string[]; categories: Record<string, number>; baseline_decisions: Record<string, number>; candidate_decisions: Record<string, number>; expected: Record<string, number>; error_types: Record<string, number>; versions: { baseline: string; candidate: string }; exposure_requested_amount: number };
  risk_acceptance: { owner_email: string; rationale: string; authorised_by: string; authorised_at: string; expires_on: string | null } | null;
  closure: { reason: string; rationale: string; closed_by: string } | null;
  retest_history: { run_number: number; candidate_version: string; passed: boolean; outcomes: Record<string, number>; at: string }[];
  history: { at: string; by: string; event: string; [k: string]: unknown }[];
  created_at: string; updated_at: string;
  executions?: (Execution & { violations: Violation[]; error: { type: string; detail: string } | null; explained_by: string[] })[];
}

export interface Review { id: string; decision: string; rationale: string; created_at: string; reviewer: { name: string; email: string } | null; assessment_snapshot: Assessment }
export interface ArtifactRow { id: string; type: string; version: number; checksum: string; size_bytes: number; created_at: string; metadata: Record<string, unknown> }
export interface AuditEvent { id: string; seq: number; actor_label: string; action: string; resource_type: string; resource_id: string | null; outcome: string; details: Record<string, unknown>; timestamp: string; hash: string }
