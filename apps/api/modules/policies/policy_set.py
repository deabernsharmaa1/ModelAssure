"""Compiled, validated policy sets (FR-11 – FR-15).

A policy set document has this shape (all keys except `policies` optional):

{
  "label": "2026-11",                       # customer's own policy version label
  "parameters": {"min_dscr": 1.5},          # thresholds supplied by the institution
  "derived_fields": [{"name": "dscr", "operator": "div",
                      "args": ["monthly_cash_available", "monthly_debt_service"]}],
  "policies": [ {policy}, ... ],            # assertions on the candidate decision
  "change_controls": [ {rule}, ... ],       # cross-version rules (FLAG / PROHIBIT)
  "expected_changes": [ {declaration}, ...],# approved, intended behaviour changes
  "severity_rules": {"TECHNICAL_ERROR": "HIGH", ...},
  "release_criteria": {"fail_on_unresolved_severities": ["CRITICAL"], ...}
}

ModelAssure never invents thresholds: every number in a policy comes from this document.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from modules.policies.dsl import (
    ALL_DECISIONS,
    DECISIONS,
    SEVERITIES,
    Always,
    DerivedField,
    Issue,
    Leaf,
    Node,
    PolicyError,
    allowed_decisions,
    compute_derived,
    eval_node,
    iter_leaves,
    node_to_text,
    parse_condition,
    parse_derived,
    provably_exclusive,
)

DEFAULT_SEVERITY_RULES: dict[str, str] = {
    "TECHNICAL_ERROR": "HIGH",
    "UNEXPECTED_CHANGE": "MEDIUM",
    "INCONCLUSIVE": "MEDIUM",
    "INVALID_INPUT_ACCEPTED": "HIGH",
    "UNSAFE_ERROR_HANDLING": "HIGH",
    "INCONSISTENT_DUPLICATE": "HIGH",
    "EXPECTATION_MISMATCH": "HIGH",
    "VERSION_MISMATCH": "HIGH",
}

DEFAULT_RELEASE_CRITERIA: dict[str, Any] = {
    "fail_on_unresolved_severities": ["CRITICAL"],
    "mandatory_categories": [],  # empty = every category in the suite is mandatory
    "max_error_rate": 0.0,
    "max_inconclusive_rate": 0.0,
}

FINDING_TYPES = set(DEFAULT_SEVERITY_RULES) | {"POLICY_VIOLATION", "CHANGE_CONTROL_VIOLATION"}


@dataclass(frozen=True)
class Policy:
    policy_id: str
    version: int
    description: str
    when: Node
    assertion: Node
    severity: str
    enabled: bool
    precedence: int
    missing_data: str  # "inconclusive" | "not_applicable"
    allowed: frozenset[str]
    raw: dict[str, Any]

    @property
    def expected_text(self) -> str:
        if self.allowed != ALL_DECISIONS:
            excluded = sorted(ALL_DECISIONS - self.allowed, key=DECISIONS.index)
            if len(excluded) == 1:
                return f"not {excluded[0]}"
            vals = sorted(self.allowed, key=DECISIONS.index)
            return vals[0] if len(vals) == 1 else " or ".join(vals)
        return node_to_text(self.assertion)


@dataclass(frozen=True)
class ChangeControl:
    rule_id: str
    description: str
    when: Node
    from_: frozenset[str]
    to: frozenset[str]
    action: str  # FLAG | PROHIBIT
    severity: str


@dataclass(frozen=True)
class ExpectedChange:
    change_id: str
    description: str
    when: Node
    from_: frozenset[str]
    to: frozenset[str]
    reference: str


@dataclass
class CompiledPolicySet:
    label: str
    parameters: dict[str, Any]
    derived: list[DerivedField]
    policies: list[Policy]
    change_controls: list[ChangeControl]
    expected_changes: list[ExpectedChange]
    severity_rules: dict[str, str]
    release_criteria: dict[str, Any]
    issues: list[Issue] = field(default_factory=list)

    @property
    def enabled_policies(self) -> list[Policy]:
        return [p for p in self.policies if p.enabled]

    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    def by_id(self, policy_id: str) -> Policy | None:
        return next((p for p in self.policies if p.policy_id == policy_id), None)


def _decision_list(v: Any, where: str) -> frozenset[str]:
    if v in (None, "*", ["*"]):
        return ALL_DECISIONS
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not v:
        raise PolicyError("must be a decision or list of decisions", where)
    bad = [x for x in v if x not in DECISIONS]
    if bad:
        raise PolicyError(f"unknown decision value(s) {bad}; use {list(DECISIONS)}", where)
    return frozenset(v)


def _severity(v: Any, where: str) -> str:
    if v not in SEVERITIES:
        raise PolicyError(f"severity must be one of {list(SEVERITIES)}", where)
    return v


def compile_policy_set(doc: dict[str, Any]) -> CompiledPolicySet:
    """Parse and validate. Structural errors raise PolicyError; semantic problems
    (conflicts, unknown parameters, unused rules) are returned as issues."""
    if not isinstance(doc, dict):
        raise PolicyError("policy set must be an object")
    allowed_keys = {"label", "parameters", "derived_fields", "policies", "change_controls",
                    "expected_changes", "severity_rules", "release_criteria", "description",
                    "source_template"}  # informational provenance; never affects evaluation
    extra = set(doc) - allowed_keys
    if extra:
        raise PolicyError(f"unsupported top-level keys {sorted(extra)}")

    params = doc.get("parameters") or {}
    if not isinstance(params, dict):
        raise PolicyError("parameters must be an object")
    derived = parse_derived(doc.get("derived_fields"))
    derived_names = {d.name for d in derived}

    policies: list[Policy] = []
    seen: set[str] = set()
    raw_policies = doc.get("policies")
    if not isinstance(raw_policies, list) or not raw_policies:
        raise PolicyError("policies must be a non-empty list")
    for i, p in enumerate(raw_policies):
        where = f"policies[{i}]"
        if not isinstance(p, dict):
            raise PolicyError("must be an object", where)
        pid = p.get("policy_id")
        if not isinstance(pid, str) or not pid:
            raise PolicyError("policy_id is required", where)
        if pid in seen:
            raise PolicyError(f"duplicate policy_id '{pid}'", where)
        seen.add(pid)
        known = {"policy_id", "version", "description", "when", "assert", "severity", "enabled",
                 "precedence", "missing_data", "explanation"}
        extra = set(p) - known
        if extra:
            raise PolicyError(f"unsupported keys {sorted(extra)}", where)
        if "assert" not in p:
            raise PolicyError("'assert' is required", where)
        when = parse_condition(p.get("when"), "input", f"{where}.when")
        assertion = parse_condition(p["assert"], "output", f"{where}.assert")
        if isinstance(assertion, Always):
            raise PolicyError("'assert' cannot be empty", where)
        md = p.get("missing_data", "inconclusive")
        if md not in ("inconclusive", "not_applicable"):
            raise PolicyError("missing_data must be 'inconclusive' or 'not_applicable'", where)
        version = p.get("version", 1)
        if not isinstance(version, int) or version < 1:
            raise PolicyError("version must be a positive integer", where)
        prec = p.get("precedence", 0)
        if not isinstance(prec, int):
            raise PolicyError("precedence must be an integer", where)
        for leaf in iter_leaves(assertion):
            if leaf.namespace == "output" and leaf.path == ("decision",) and leaf.value_ref is None:
                vals = leaf.value if isinstance(leaf.value, list) else [leaf.value]
                bad = [v for v in vals if v not in DECISIONS]
                if bad:
                    raise PolicyError(
                        f"unknown decision value(s) {bad}. Map customer values to {list(DECISIONS)} in the connection mapping",
                        f"{where}.assert",
                    )
        allowed = allowed_decisions(assertion)
        if not allowed:
            raise PolicyError("assertion can never be satisfied (no decision is allowed)", where)
        policies.append(Policy(
            policy_id=pid, version=version, description=str(p.get("description", "")),
            when=when, assertion=assertion, severity=_severity(p.get("severity", "HIGH"), where),
            enabled=bool(p.get("enabled", True)), precedence=prec, missing_data=md,
            allowed=allowed, raw=p,
        ))

    change_controls: list[ChangeControl] = []
    for i, r in enumerate(doc.get("change_controls") or []):
        where = f"change_controls[{i}]"
        rid = r.get("rule_id")
        if not rid or rid in seen:
            raise PolicyError("rule_id is required and must be unique", where)
        seen.add(rid)
        action = r.get("action", "FLAG")
        if action not in ("FLAG", "PROHIBIT"):
            raise PolicyError("action must be FLAG or PROHIBIT", where)
        change_controls.append(ChangeControl(
            rule_id=rid, description=str(r.get("description", "")),
            when=parse_condition(r.get("when"), "input", f"{where}.when"),
            from_=_decision_list(r.get("from"), f"{where}.from"), to=_decision_list(r.get("to"), f"{where}.to"),
            action=action, severity=_severity(r.get("severity", "MEDIUM"), where),
        ))

    expected: list[ExpectedChange] = []
    for i, r in enumerate(doc.get("expected_changes") or []):
        where = f"expected_changes[{i}]"
        cid = r.get("change_id")
        if not cid or cid in seen:
            raise PolicyError("change_id is required and must be unique", where)
        seen.add(cid)
        expected.append(ExpectedChange(
            change_id=cid, description=str(r.get("description", "")),
            when=parse_condition(r.get("when"), "input", f"{where}.when"),
            from_=_decision_list(r.get("from"), f"{where}.from"), to=_decision_list(r.get("to"), f"{where}.to"),
            reference=str(r.get("reference", "")),
        ))

    sev_rules = dict(DEFAULT_SEVERITY_RULES)
    for k, v in (doc.get("severity_rules") or {}).items():
        if k not in FINDING_TYPES:
            raise PolicyError(f"unknown finding type '{k}' in severity_rules")
        sev_rules[k] = _severity(v, f"severity_rules.{k}")

    criteria = dict(DEFAULT_RELEASE_CRITERIA)
    for k, v in (doc.get("release_criteria") or {}).items():
        if k not in DEFAULT_RELEASE_CRITERIA:
            raise PolicyError(f"unknown release criterion '{k}'")
        criteria[k] = v
    for s in criteria["fail_on_unresolved_severities"]:
        _severity(s, "release_criteria.fail_on_unresolved_severities")

    cps = CompiledPolicySet(
        label=str(doc.get("label", "")), parameters=params, derived=derived, policies=policies,
        change_controls=change_controls, expected_changes=expected,
        severity_rules=sev_rules, release_criteria=criteria,
    )
    seen_issues, unique = set(), []
    for i in _semantic_checks(cps, derived_names):
        key = (i.level, i.code, i.message)
        if key not in seen_issues:
            seen_issues.add(key)
            unique.append(i)
    cps.issues = unique
    return cps


def _all_nodes(cps: CompiledPolicySet):
    for p in cps.policies:
        yield p.policy_id, p.when
        yield p.policy_id, p.assertion
    for r in cps.change_controls:
        yield r.rule_id, r.when
    for r in cps.expected_changes:
        yield r.change_id, r.when


def _semantic_checks(cps: CompiledPolicySet, derived_names: set[str]) -> list[Issue]:
    issues: list[Issue] = []
    for rid, node in _all_nodes(cps):
        for leaf in iter_leaves(node):
            refs = [(leaf.namespace, leaf.path)]
            if leaf.value_ref:
                refs.append(leaf.value_ref)
            for ns, path in refs:
                if ns == "params" and path[0] not in cps.parameters:
                    issues.append(Issue("error", "UNKNOWN_PARAMETER", f"{rid} references undefined parameter 'params.{path[0]}'", [rid]))
                elif ns == "params" and cps.parameters.get(path[0]) is None:
                    issues.append(Issue("error", "PARAMETER_NOT_SET",
                                        f"{rid} needs a value for 'params.{path[0]}'. Thresholds must come from your approved policy.", [rid]))
                if ns == "derived" and path[0] not in derived_names:
                    issues.append(Issue("error", "UNKNOWN_DERIVED_FIELD", f"{rid} references undefined derived field 'derived.{path[0]}'", [rid]))
            if leaf.value_ref and leaf.value_ref[0] == "params" and leaf.value_ref[1][0] in cps.parameters:
                pv = cps.parameters[leaf.value_ref[1][0]]
                if pv is None:
                    issues.append(Issue("error", "PARAMETER_NOT_SET",
                                        f"{rid} needs a value for 'params.{leaf.value_ref[1][0]}'. Thresholds must come from your approved policy.", [rid]))
                elif leaf.operator in ("gt", "gte", "lt", "lte") and (isinstance(pv, bool) or not isinstance(pv, (int, float))):
                    issues.append(Issue("error", "PARAMETER_TYPE", f"{rid}: parameter '{leaf.value_ref[1][0]}' must be numeric for '{leaf.operator}'", [rid]))

    # FR-13: conflicting policies. Two enabled policies conflict when they can both apply,
    # accept disjoint decision sets, and have the same precedence (so neither can win).
    enabled = cps.enabled_policies
    for i, a in enumerate(enabled):
        for b in enabled[i + 1:]:
            if a.allowed & b.allowed:
                continue
            if provably_exclusive(a.when, b.when):
                continue
            if a.precedence == b.precedence:
                issues.append(Issue(
                    "error", "POLICY_CONFLICT",
                    f"{a.policy_id} requires {a.expected_text} and {b.policy_id} requires {b.expected_text}; "
                    f"both can apply to the same case and have equal precedence ({a.precedence}). "
                    f"Make their conditions mutually exclusive or set different precedence.",
                    [a.policy_id, b.policy_id],
                ))
            else:
                hi, lo = (a, b) if a.precedence > b.precedence else (b, a)
                issues.append(Issue(
                    "warning", "POLICY_SUPERSEDED",
                    f"When both apply, {hi.policy_id} (precedence {hi.precedence}) takes priority over "
                    f"{lo.policy_id} (precedence {lo.precedence}).",
                    [hi.policy_id, lo.policy_id],
                ))
    for p in cps.policies:
        if not p.enabled:
            issues.append(Issue("warning", "POLICY_DISABLED", f"{p.policy_id} is disabled and will not be evaluated", [p.policy_id]))
    for r in cps.expected_changes:
        if r.from_ & r.to == r.from_ and r.from_ == r.to:
            issues.append(Issue("warning", "NO_OP_CHANGE", f"{r.change_id} declares a change from and to the same decisions", [r.change_id]))
    return issues


# --------------------------------------------------------------------------------------------
# Evaluation against one case
# --------------------------------------------------------------------------------------------
NOT_APPLICABLE = "NOT_APPLICABLE"
SATISFIED = "SATISFIED"
VIOLATED = "VIOLATED"
UNKNOWN = "UNKNOWN"
SUPERSEDED = "SUPERSEDED"


@dataclass
class PolicyOutcome:
    policy_id: str
    version: int
    status: str
    severity: str
    expected: str
    observed: Any
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"policy_id": self.policy_id, "version": self.version, "status": self.status,
                "severity": self.severity, "expected": self.expected, "observed": self.observed,
                "reason": self.reason}


def policy_fingerprint(cps: CompiledPolicySet, p: Policy) -> str:
    """Identity of a policy's effective behaviour: its definition plus the parameter values and
    derived-field definitions it depends on. Used to tell an approved revision from an unchanged rule."""
    from common.hashing import content_hash

    params: dict[str, Any] = {}
    derived: list[str] = []
    for node in (p.when, p.assertion):
        for leaf in iter_leaves(node):
            for ns, path in [(leaf.namespace, leaf.path)] + ([leaf.value_ref] if leaf.value_ref else []):
                if ns == "params":
                    params[path[0]] = cps.parameters.get(path[0])
                if ns == "derived":
                    derived.append(path[0])
    dmap = {d.name: d for d in cps.derived}
    return content_hash({
        "raw": p.raw, "params": params,
        "derived": [repr(dmap.get(n)) for n in sorted(set(derived))],
    })


def build_context(cps: CompiledPolicySet, case_input: Any, scenario: dict[str, Any],
                  output: dict[str, Any] | None, baseline: dict[str, Any] | None) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "input": case_input if isinstance(case_input, dict) else {},
        "scenario": scenario or {},
        "params": cps.parameters,
    }
    if output is not None:
        ctx["output"] = output
    if baseline is not None:
        ctx["baseline"] = baseline
    ctx["derived"] = compute_derived(cps.derived, ctx)
    return ctx


def evaluate_policies(cps: CompiledPolicySet, ctx: dict[str, Any]) -> list[PolicyOutcome]:
    observed = ctx.get("output", {}).get("decision")
    outcomes: list[PolicyOutcome] = []
    for p in cps.enabled_policies:
        applies = eval_node(p.when, ctx)
        if applies is False:
            outcomes.append(PolicyOutcome(p.policy_id, p.version, NOT_APPLICABLE, p.severity, p.expected_text, observed))
            continue
        if applies is None:
            if p.missing_data == "not_applicable":
                outcomes.append(PolicyOutcome(p.policy_id, p.version, NOT_APPLICABLE, p.severity, p.expected_text, observed,
                                              "condition fields missing; policy configured as not applicable"))
            else:
                outcomes.append(PolicyOutcome(p.policy_id, p.version, UNKNOWN, p.severity, p.expected_text, observed,
                                              f"cannot determine whether policy applies: {node_to_text(p.when)}"))
            continue
        ok = eval_node(p.assertion, ctx)
        if ok is None:
            outcomes.append(PolicyOutcome(p.policy_id, p.version, UNKNOWN, p.severity, p.expected_text, observed,
                                          f"cannot evaluate assertion: {node_to_text(p.assertion)}"))
        else:
            outcomes.append(PolicyOutcome(p.policy_id, p.version, SATISFIED if ok else VIOLATED, p.severity,
                                          p.expected_text, observed, p.description))

    # Precedence: among policies that definitely apply, a higher-precedence policy supersedes a
    # lower one whose accepted decisions are disjoint from it. Equal precedence → conflict.
    by_id = {p.policy_id: p for p in cps.enabled_policies}
    applicable = [o for o in outcomes if o.status in (SATISFIED, VIOLATED)]
    for o in applicable:
        mine = by_id[o.policy_id]
        for other in applicable:
            if other is o:
                continue
            theirs = by_id[other.policy_id]
            if mine.allowed & theirs.allowed:
                continue
            if theirs.precedence > mine.precedence:
                o.status = SUPERSEDED
                o.reason = f"superseded by {theirs.policy_id} (higher precedence)"
                break
            if theirs.precedence == mine.precedence:
                o.status = UNKNOWN
                o.reason = f"conflicts with {theirs.policy_id} at equal precedence"
                break
    return outcomes
