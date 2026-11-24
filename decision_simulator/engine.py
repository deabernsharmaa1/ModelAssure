"""Decision logic of the fictional lender "Tanjung Kredit Berhad" (SME working-capital lending).

Everything here is invented for demonstration. It is NOT a credit model.

Builds
  3.1.0  baseline   policy 2026-06   min DSCR 1.25
  3.2.0  candidate  policy 2026-11   min DSCR 1.50 (approved change), recalibrated PD curve, and two
                                     seeded defects:
           D1  bureau data unavailable / timed out -> silently assumes a default score and can APPROVE
           D2  unresolved HIGH fraud alert is overridden for very low-PD applicants -> APPROVE
  3.2.1  candidate  same as 3.2.0 with D1 and D2 fixed (used to demonstrate fix-and-retest)
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Build:
    model_version: str
    policy_version: str
    min_dscr: float
    pd_midpoint: float
    defect_bureau_fallback: bool
    defect_fraud_override: bool


BUILDS: dict[str, Build] = {
    "3.1.0": Build("3.1.0", "2026-06", 1.25, 550.0, False, False),
    "3.2.0": Build("3.2.0", "2026-11", 1.50, 548.0, True, True),
    "3.2.1": Build("3.2.1", "2026-11", 1.50, 548.0, False, False),
}

STATUS_OUT = {"APPROVE": "APPROVED", "DECLINE": "DECLINED", "REVIEW": "MANUAL_REVIEW",
              "HOLD": "ON_HOLD", "REFER": "REFERRED"}


def default_bureau_score(application_id: str) -> int:
    """Deterministic pseudo-bureau score for live lookups without an explicit fixture."""
    h = int(hashlib.sha256(application_id.encode()).hexdigest()[:8], 16)
    return 480 + h % 360  # 480..839


def probability_of_default(score: float, dscr: float, midpoint: float) -> float:
    base = 1.0 / (1.0 + math.exp((score - midpoint) / 55.0))
    adj = 1.0 if dscr >= 2.0 else 1.0 + (2.0 - dscr) * 0.6
    return round(min(0.99, base * adj), 4)


def decide(build: Build, req: dict[str, Any], bureau: dict[str, Any], fraud: dict[str, Any],
           identity: dict[str, Any]) -> tuple[str, float | None, list[str]]:
    """Returns (decision, pd, reason_codes). `bureau/fraud/identity` are the effective dependency
    results, each with a `status` ("AVAILABLE"/"UNAVAILABLE"/"TIMEOUT", "CLEAR"/"MEDIUM"/"HIGH", ...)."""
    reasons: list[str] = []
    cash = req.get("monthlyCashAvailable")
    debt = req.get("monthlyDebtService")
    dscr = (cash / debt) if (isinstance(cash, (int, float)) and isinstance(debt, (int, float)) and debt > 0) else None

    fraud_status = fraud.get("status")
    fraud_resolved = bool(fraud.get("alertResolved", False))
    bureau_status = bureau.get("status")
    score = bureau.get("score")

    # Indicative PD where we can compute it (used by the D2 defect and reported back).
    effective_score = score if (bureau_status == "AVAILABLE" and isinstance(score, (int, float))) else None
    if effective_score is None and build.defect_bureau_fallback and bureau_status in ("UNAVAILABLE", "TIMEOUT"):
        effective_score = 720  # D1: silently assume a "typical" score instead of referring to review
        reasons.append("BUREAU_DEFAULT_SCORE_APPLIED")
    pd = probability_of_default(effective_score, dscr, build.pd_midpoint) if (effective_score is not None and dscr is not None) else None

    # 1. Unresolved high-risk fraud alert -> hold for investigation
    if fraud_status == "HIGH" and not fraud_resolved:
        if build.defect_fraud_override and pd is not None and pd < 0.03:
            reasons.append("FRAUD_ALERT_OVERRIDDEN_PRIME")  # D2
            return "APPROVE", pd, reasons + ["LOW_CREDIT_RISK"]
        return "HOLD", pd, reasons + ["FRAUD_ALERT_UNRESOLVED"]

    # 2. Dependency timeouts -> approved fallback (manual review)
    if fraud_status == "TIMEOUT" or identity.get("status") == "TIMEOUT":
        return "REVIEW", pd, reasons + ["DEPENDENCY_TIMEOUT"]
    if bureau_status in ("UNAVAILABLE", "TIMEOUT") and not build.defect_bureau_fallback:
        return "REVIEW", None, reasons + (["DEPENDENCY_TIMEOUT"] if bureau_status == "TIMEOUT" else ["BUREAU_UNAVAILABLE"])

    # 3. Missing financials -> manual review
    if dscr is None:
        return "REVIEW", None, reasons + ["FINANCIALS_INCOMPLETE"]

    # 4. Affordability (customer-approved DSCR limit)
    if dscr < 1.0:
        return "DECLINE", pd, reasons + ["DSCR_BELOW_1"]
    if dscr < build.min_dscr:
        return "REFER", pd, reasons + ["DSCR_BELOW_LIMIT"]

    # 5. Identity must be verified before any automatic approval
    if identity.get("status") != "VERIFIED":
        return "REVIEW", pd, reasons + ["IDENTITY_INCOMPLETE"]

    # 6. Fraud medium -> review
    if fraud_status == "MEDIUM":
        return "REVIEW", pd, reasons + ["FRAUD_ALERT_MEDIUM"]

    # 7. Credit risk
    if pd is None:
        return "REVIEW", None, reasons + ["SCORE_UNAVAILABLE"]
    if pd > 0.20:
        return "DECLINE", pd, reasons + ["HIGH_CREDIT_RISK"]
    if pd > 0.08:
        return "REFER", pd, reasons + ["ELEVATED_CREDIT_RISK"]
    return "APPROVE", pd, reasons + ["LOW_CREDIT_RISK"]
