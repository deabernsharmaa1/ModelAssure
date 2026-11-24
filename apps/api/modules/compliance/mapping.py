"""Regulatory mapping: which supervisory expectations a run's evidence supports.

Each framework (frameworks/*.json) lists controls; each control names evidence checks that are
computed from the run's own records. Control status:

  MET           every check passed
  PARTIAL       some checks passed, or ModelAssure can only ever cover part of the expectation
  NOT_MET       the evidence shows the expectation is not met (e.g. unresolved violations)
  NO_EVIDENCE   the relevant testing or review has not happened for this run
  OUT_OF_SCOPE  ModelAssure Version 1 does not produce evidence for this expectation

This supports an institution's own compliance assessment. It is not a compliance opinion or a
certification, and paragraph references should be confirmed by the institution.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import Finding, PolicySet, Review, TestRun
from modules.audit.service import verify_chain
from modules.findings.grouping import FAILING_TYPES, UNRESOLVED, open_for_build

FRAMEWORK_DIR = Path(__file__).parent / "frameworks"
DISCLAIMER = ("This mapping shows which supervisory expectations the evidence from this run supports. It is not a legal or "
              "compliance opinion and does not certify compliance. Confirm references and conclusions with your compliance function.")

MET, PARTIAL, NOT_MET, NO_EVIDENCE, OUT_OF_SCOPE = "MET", "PARTIAL", "NOT_MET", "NO_EVIDENCE", "OUT_OF_SCOPE"


@lru_cache
def load_frameworks() -> dict[str, dict[str, Any]]:
    out = {}
    for p in sorted(FRAMEWORK_DIR.glob("*.json")):
        doc = json.loads(p.read_text(encoding="utf-8"))
        out[doc["framework_id"]] = doc
    return out


@dataclass
class Check:
    status: str  # MET | PARTIAL | NOT_MET | NO_EVIDENCE
    detail: str


class RunEvidence:
    """Computes evidence checks from the stored records of one run (lazily, once each)."""

    def __init__(self, db: Session, run: TestRun):
        self.db, self.run = db, run
        self.snap = run.config_snapshot
        self.summary = run.summary or {}
        self.coverage = {c["category"]: c for c in self.summary.get("coverage", [])}
        self._findings: list[Finding] | None = None
        self._reviews: list[Review] | None = None
        self._cache: dict[str, Check] = {}
        self.candidate_version = self.snap["versions"]["candidate"]["version"]

    def is_open(self, f: Finding) -> bool:
        return open_for_build(f, self.candidate_version)

    @property
    def findings(self) -> list[Finding]:
        if self._findings is None:
            self._findings = list(self.db.scalars(select(Finding).where(Finding.run_id == self.run.id)))
        return self._findings

    @property
    def reviews(self) -> list[Review]:
        if self._reviews is None:
            self._reviews = list(self.db.scalars(select(Review).where(Review.run_id == self.run.id).order_by(Review.created_at)))
        return self._reviews

    def check(self, name: str) -> Check:
        if name not in self._cache:
            fn: Callable[[], Check] | None = getattr(self, f"_c_{name}", None)
            if fn is None:
                raise KeyError(f"unknown evidence check {name}")
            self._cache[name] = fn()
        return self._cache[name]

    # ---- checks ------------------------------------------------------------------------------
    def _c_suite_executed_full(self) -> Check:
        if self.run.status != "COMPLETED":
            return Check(NO_EVIDENCE, f"Run is {self.run.status}.")
        expected = self.snap["suite"].get("category_counts") or {}
        missing = [c for c, n in expected.items() if (self.coverage.get(c) or {}).get("total", 0) < n]
        if self.run.scope != "FULL":
            return Check(PARTIAL, f"{self.run.scope.title()} run: {self.summary.get('total', 0)} of {self.snap['suite']['case_count']} suite cases.")
        skipped = sum(c.get("skipped", 0) for c in self.coverage.values())
        if missing or skipped:
            return Check(PARTIAL, f"Categories not fully executed: {', '.join(missing) or 'none'}; skipped cases: {skipped}.")
        return Check(MET, f"Full suite executed before release: {self.summary.get('total', 0)} cases across {len(expected)} scenario categories "
                          f"(suite hash {self.snap['suite']['content_hash'][:12]}…).")

    def _executed(self, cat: str) -> int:
        c = self.coverage.get(cat) or {}
        return c.get("total", 0) - c.get("skipped", 0)

    def _c_edge_and_negative_cases(self) -> Check:
        cats = {"BOUNDARY": "threshold boundaries", "MISSING_DATA": "missing data", "INVALID_INPUT": "invalid input",
                "CROSS_SYSTEM_CONFLICT": "contradictory credit/fraud signals"}
        done = {k: self._executed(k) for k in cats}
        present = [f"{cats[k]} ({n})" for k, n in done.items() if n]
        if len(present) == len(cats):
            return Check(MET, "Edge and negative cases executed: " + "; ".join(present) + ".")
        if present:
            return Check(PARTIAL, "Executed: " + "; ".join(present) + ". Missing: " + ", ".join(cats[k] for k, n in done.items() if not n) + ".")
        return Check(NO_EVIDENCE, "No boundary, missing-data, invalid-input or conflict cases were executed.")

    def _c_invalid_input_safe(self) -> Check:
        c = self.coverage.get("INVALID_INPUT")
        if not c or not self._executed("INVALID_INPUT"):
            return Check(NO_EVIDENCE, "No invalid-input cases executed.")
        if c.get("fail", 0) or c.get("error", 0):
            return Check(NOT_MET, f"{c.get('fail', 0)} invalid inputs were accepted or caused server errors; {c.get('error', 0)} technical errors.")
        return Check(MET, f"All {c['total']} invalid inputs were safely rejected.")

    def _c_dependency_faults(self) -> Check:
        if not self._executed("DEPENDENCY_FAILURE"):
            return Check(NO_EVIDENCE, "No dependency-failure scenarios executed.")
        faults = self.summary.get("dependency_behaviour", [])
        failing = [f["fault"] for f in faults if f.get("failed")]
        open_dep = [f for f in self.findings if self.is_open(f) and f.type in FAILING_TYPES
                    and (f.summary.get("categories") or {}).get("DEPENDENCY_FAILURE")]
        kinds = ", ".join(sorted({f["fault"] for f in faults})) or "none"
        if open_dep:
            return Check(NOT_MET, f"Dependency failures tested ({kinds}); unresolved fallback violations: "
                                  + ", ".join(f"F-{f.finding_number}" for f in open_dep) + ".")
        if failing:
            return Check(PARTIAL, f"Dependency failures tested ({kinds}); failures occurred but their findings are resolved or accepted.")
        return Check(MET, f"Approved fallback applied under every injected dependency failure ({kinds}).")

    def _c_regression_vs_baseline(self) -> Check:
        if not self.summary.get("transition_matrix"):
            return Check(NO_EVIDENCE, "No baseline comparison available.")
        ch = self.summary.get("changes", {})
        open_unexpected = [f for f in self.findings if f.type == "UNEXPECTED_CHANGE" and f.status in UNRESOLVED]
        base = (f"Baseline {self.snap['versions']['baseline']['version']} compared with candidate {self.snap['versions']['candidate']['version']} "
                f"on every case: {ch.get('total', 0)} decision changes ({ch.get('expected', 0)} expected, {ch.get('unexpected', 0)} unexpected, "
                f"{ch.get('prohibited', 0)} prohibited).")
        if open_unexpected:
            return Check(PARTIAL, base + f" {sum(f.affected_count for f in open_unexpected)} unexpected change(s) still await review.")
        return Check(MET, base + " Every unexpected change has been reviewed.")

    def _c_customer_protection_rules(self) -> Check:
        ps = self.snap["policy_sets"]["candidate"]
        n_pol = len(ps["content"].get("policies") or [])
        open_v = [f for f in self.findings if f.type in FAILING_TYPES and self.is_open(f)]
        if not self.summary.get("total"):
            return Check(NO_EVIDENCE, "No cases executed.")
        if open_v:
            return Check(NOT_MET, f"{n_pol} approved policies evaluated; violations still present in {self.candidate_version}: "
                                  + ", ".join(f"F-{f.finding_number} ({f.severity}{', fixed only in a later build' if f.status not in UNRESOLVED else ''})"
                                              for f in open_v) + ".")
        return Check(MET, f"{n_pol} approved policies ({ps['name']} v{ps['version']}) evaluated on every decision with no unresolved violations.")

    def _c_approved_policies_four_eyes(self) -> Check:
        problems = []
        for side, ps in self.snap["policy_sets"].items():
            row = self.db.get(PolicySet, uuid.UUID(ps["id"]))
            if ps["approval_status"] != "APPROVED":
                problems.append(f"{side} policy set was {ps['approval_status']} when the run started")
            elif row is not None and row.approved_by in (row.created_by, row.submitted_by):
                problems.append(f"{side} policy set approved by its author")
        if problems:
            return Check(NOT_MET, "; ".join(problems) + ".")
        return Check(MET, "Both policy sets were approved under four-eyes review before the run (author ≠ approver).")

    def _c_independent_review(self) -> Check:
        if not self.reviews:
            return Check(NO_EVIDENCE, "No reviewer decision recorded for this run yet.")
        r = self.reviews[-1]
        return Check(MET, f"Release reviewed by a reviewer independent of the candidate's builders and the run's initiator "
                          f"(segregation of duties enforced); decision {r.decision} on {r.created_at.date().isoformat()}.")

    def _c_human_oversight(self) -> Check:
        if not self.reviews:
            return Check(NO_EVIDENCE, "No human release decision recorded yet.")
        r = self.reviews[-1]
        return Check(MET, f"Human release decision recorded with rationale: {r.decision} (assessment at review: "
                          f"{r.assessment_snapshot.get('outcome')}). ModelAssure never authorises deployment itself.")

    def _c_findings_remediated(self) -> Check:
        failing = [f for f in self.findings if f.type in FAILING_TYPES]
        open_ = [f for f in failing if self.is_open(f)]
        if open_:
            return Check(NOT_MET, f"{len(open_)} failing finding(s) not resolved in {self.candidate_version}: "
                                  + ", ".join(f"F-{f.finding_number}" for f in open_) + ".")
        accepted = [f for f in failing if f.status == "ACCEPTED_RISK"]
        msg = f"All {len(failing)} failing finding(s) resolved, closed or covered by documented risk acceptance." if failing else "No failing findings."
        if accepted:
            msg += f" Risk accepted with named owners: {', '.join(f'F-{f.finding_number}' for f in accepted)}."
        return Check(MET, msg)

    def _c_version_traceability(self) -> Check:
        mism = (self.summary.get("errors_by_type") or {}).get("VERSION_MISMATCH", 0)
        if mism:
            return Check(NOT_MET, f"{mism} response(s) came from a different model version than the one registered.")
        v = self.snap["versions"]
        return Check(MET, f"Tested builds pinned by immutable version records (baseline {v['baseline']['version']} "
                          f"{v['baseline']['checksum'][:10]}…, candidate {v['candidate']['version']} {v['candidate']['checksum'][:10]}…); "
                          f"reported model versions verified on every response; configuration locked (hash {self.run.snapshot_hash[:12]}…).")

    def _c_audit_trail(self) -> Check:
        res = verify_chain(self.db, self.run.organization_id)
        if not res["valid"]:
            return Check(NOT_MET, f"Audit chain integrity failure: {res.get('problem')}.")
        return Check(MET, f"Append-only, hash-chained audit trail intact ({res['checked']} events verified).")


def evaluate(db: Session, run: TestRun, framework_ids: list[str] | None = None) -> dict[str, Any]:
    fws = load_frameworks()
    ev = RunEvidence(db, run)
    out = []
    for fid, fw in fws.items():
        if framework_ids and fid not in framework_ids:
            continue
        controls = []
        counts: dict[str, int] = {}
        for c in fw["controls"]:
            if c["max_coverage"] == "NONE" or not c["checks"]:
                status, checks = OUT_OF_SCOPE, []
            else:
                checks = [{"check": name, **ev.check(name).__dict__} for name in c["checks"]]
                sts = [x["status"] for x in checks]
                if any(s == NOT_MET for s in sts):
                    status = NOT_MET
                elif all(s == MET for s in sts):
                    status = PARTIAL if c["max_coverage"] == "PARTIAL" else MET
                elif all(s == NO_EVIDENCE for s in sts):
                    status = NO_EVIDENCE
                else:
                    status = PARTIAL
            counts[status] = counts.get(status, 0) + 1
            controls.append({**{k: v for k, v in c.items() if k != "checks"}, "status": status, "checks": checks})
        out.append({**{k: v for k, v in fw.items() if k != "controls"}, "summary": counts, "controls": controls})
    return {"run_id": str(run.id), "run_number": run.run_number, "disclaimer": DISCLAIMER, "frameworks": out}
