"""Deterministic scenario generation (Module D, spec §8.2).

Cases are generated ONCE, when a test-suite version is created, then stored and hashed. Runs only
replay stored cases, so a run is reproducible even though the property generator uses Hypothesis.

Every generated case is a dict:
  {"case_key", "category", "input", "scenario": {"faults": {...}, "repeat": n?}, "expected_behavior", "tags"}
"""
from __future__ import annotations

import copy
import re
from typing import Any, Callable

from common.hashing import content_hash
from modules.policies.dsl import iter_leaves
from modules.policies.policy_set import CompiledPolicySet

DEPENDENCIES = ("bureau", "fraud", "identity")
FAULT_MODES = ("TIMEOUT", "UNAVAILABLE", "MALFORMED", "MISSING_RESPONSE")


class GenerationError(ValueError):
    pass


CASE_KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


def default_scenario(**overrides: Any) -> dict[str, Any]:
    sc: dict[str, Any] = {"faults": {d: "NONE" for d in DEPENDENCIES}}
    for k, v in overrides.items():
        if k == "faults":
            sc["faults"].update(v)
        else:
            sc[k] = v
    return sc


DEFAULT_BASE_CASE: dict[str, Any] = {
    "product": "SME_WORKING_CAPITAL",
    "requested_amount": 100000,
    "currency": "MYR",
    "monthly_cash_available": 25000,
    "monthly_debt_service": 12000,
    "bureau": {"status": "AVAILABLE", "score": 760},
    "fraud": {"status": "CLEAR"},
    "identity": {"status": "VERIFIED"},
}

# Credit profiles used across generators: (label, bureau score)
PROFILES = (("prime", 790), ("standard", 700), ("near_prime", 620))


def _with(base: dict[str, Any], **changes: Any) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for path, value in changes.items():
        parts = path.split("__")
        cur = out
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        if value is _DROP:
            cur.pop(parts[-1], None)
        else:
            cur[parts[-1]] = value
    return out


_DROP = object()


def _financials(dscr: float, debt: float = 12000) -> dict[str, float]:
    return {"monthly_cash_available": round(dscr * debt, 2), "monthly_debt_service": debt}


# --------------------------------------------------------------------------------------------
def gen_normal(base: dict, cfg: dict) -> list[dict]:
    cases = []
    amounts = cfg.get("amounts", [50000, 150000, 400000])
    for label, score in PROFILES:
        for dscr in (1.8, 2.4, 3.2):
            for amt in amounts:
                inp = _with(base, requested_amount=amt, bureau__score=score, **_flat(_financials(dscr)))
                cases.append(_case("NORMAL", inp, tags=[label, f"dscr={dscr}"]))
    return cases[: cfg.get("count", len(cases))]


def _flat(d: dict[str, Any]) -> dict[str, Any]:
    return dict(d)


def gen_boundary(base: dict, cfg: dict, policy_sets: list[CompiledPolicySet]) -> list[dict]:
    """Values immediately below, at and above every numeric threshold found in the supplied
    policy sets (literal values and numeric parameters)."""
    thresholds: dict[tuple[str, ...], set[float]] = {}
    derived_defs: dict[str, Any] = {}
    for cps in policy_sets:
        for d in cps.derived:
            derived_defs[d.name] = d
        nodes = [p.when for p in cps.enabled_policies] + [c.when for c in cps.change_controls] + [e.when for e in cps.expected_changes]
        for node in nodes:
            for leaf in iter_leaves(node):
                if leaf.operator not in ("gt", "gte", "lt", "lte", "eq"):
                    continue
                t = None
                if leaf.value_ref and leaf.value_ref[0] == "params":
                    t = cps.parameters.get(leaf.value_ref[1][0])
                elif isinstance(leaf.value, (int, float)) and not isinstance(leaf.value, bool):
                    t = leaf.value
                if isinstance(t, (int, float)) and leaf.namespace in ("input", "derived"):
                    thresholds.setdefault((leaf.namespace, *leaf.path), set()).add(float(t))
    eps_cfg = cfg.get("epsilon", {})
    cases: list[dict] = []
    for ref, ts in sorted(thresholds.items()):
        name = ref[-1]
        for t in sorted(ts):
            eps = float(eps_cfg.get(name, 0.01 if abs(t) < 100 else 1))
            for v in (round(t - eps, 6), t, round(t + eps, 6)):
                for label, score in PROFILES:
                    inp = _with(base, bureau__score=score)
                    if ref[0] == "derived":
                        d = derived_defs.get(ref[1])
                        if d is None or d.operator != "div" or not all(isinstance(a, tuple) and a[0] == "input" for a in d.args):
                            continue
                        num_path, den_path = d.args[0][1], d.args[1][1]
                        den = _get(inp, den_path)
                        if not isinstance(den, (int, float)) or den == 0:
                            continue
                        _set(inp, num_path, round(v * den, 2))
                    else:
                        _set(inp, ref[1:], v)
                    cases.append(_case("BOUNDARY", inp, tags=[label, f"{'.'.join(ref)}={v}", f"threshold={t}"]))
    return cases


def _get(obj: dict, path: tuple[str, ...]):
    cur: Any = obj
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _set(obj: dict, path: tuple[str, ...], value: Any) -> None:
    cur = obj
    for p in path[:-1]:
        cur = cur.setdefault(p, {})
    cur[path[-1]] = value


def gen_missing_data(base: dict, cfg: dict) -> list[dict]:
    cases = []
    drops = cfg.get("drop", [["monthly_cash_available"], ["monthly_debt_service"],
                             ["monthly_cash_available", "monthly_debt_service"]])
    for label, score in PROFILES:
        for dscr in (1.8, 3.0):
            for fields in drops:
                inp = _with(base, bureau__score=score, **_financials(dscr))
                for f in fields:
                    inp.pop(f, None)
                cases.append(_case("MISSING_DATA", inp, tags=[label, "missing=" + "+".join(fields)]))
    # Mandatory bureau data reported unavailable by the upstream system
    for label, score in PROFILES:
        for dscr in (1.3, 1.6, 2.0, 2.6):
            for amt in (60000, 250000):
                inp = _with(base, requested_amount=amt, bureau={"status": "UNAVAILABLE"}, **_financials(dscr))
                cases.append(_case("MISSING_DATA", inp, tags=[label, "bureau=UNAVAILABLE", f"dscr={dscr}"]))
    return cases


def gen_dependency_failure(base: dict, cfg: dict) -> list[dict]:
    """Live dependency faults: the dependency block is omitted from the request so the decision
    system must call the (mocked) provider, which ModelAssure instructs to fail."""
    cases = []
    deps = cfg.get("dependencies", list(DEPENDENCIES))
    modes = cfg.get("modes", list(FAULT_MODES))
    for dep in deps:
        for mode in modes:
            for dscr in cfg.get("dscr_levels", (1.7, 2.2, 3.0)):
                inp = _with(base, **_financials(dscr))
                inp.pop(dep, None)
                cases.append(_case("DEPENDENCY_FAILURE", inp, scenario=default_scenario(faults={dep: mode}),
                                   tags=[f"{dep}={mode}", f"dscr={dscr}"]))
    return cases


def gen_cross_system_conflict(base: dict, cfg: dict) -> list[dict]:
    cases = []
    prime = [(820, 3.0), (800, 2.6), (780, 2.2), (760, 2.0)]
    for score, dscr in prime:
        for amt in (80000, 300000):
            fin = _financials(dscr)
            cases.append(_case("CROSS_SYSTEM_CONFLICT", _with(base, requested_amount=amt, bureau__score=score, fraud={"status": "HIGH"}, **fin),
                               tags=["credit=excellent", "fraud=HIGH unresolved"]))
            cases.append(_case("CROSS_SYSTEM_CONFLICT", _with(base, requested_amount=amt, bureau__score=score, fraud={"status": "HIGH", "alert_resolved": True}, **fin),
                               tags=["credit=excellent", "fraud=HIGH resolved"]))
            cases.append(_case("CROSS_SYSTEM_CONFLICT", _with(base, requested_amount=amt, bureau__score=score, fraud={"status": "MEDIUM"}, **fin),
                               tags=["credit=excellent", "fraud=MEDIUM"]))
            cases.append(_case("CROSS_SYSTEM_CONFLICT", _with(base, requested_amount=amt, bureau__score=score, identity={"status": "FAILED"}, **fin),
                               tags=["credit=excellent", "identity=FAILED"]))
    for score in (820, 760):
        cases.append(_case("CROSS_SYSTEM_CONFLICT", _with(base, bureau__score=score, **_financials(0.8)),
                           tags=["credit=excellent", "dscr=0.8"]))
    return cases


def gen_duplicate(base: dict, cfg: dict) -> list[dict]:
    repeat = int(cfg.get("repeat", 3))
    cases = []
    for i, (label, score) in enumerate(PROFILES):
        for dscr in (1.4, 2.0, 2.8):
            inp = _with(base, bureau__score=score, **_financials(dscr))
            cases.append(_case("DUPLICATE", inp, scenario=default_scenario(repeat=repeat), tags=[label, f"repeat={repeat}"]))
    return cases


def gen_invalid_input(base: dict, cfg: dict) -> list[dict]:
    variants: list[tuple[str, Callable[[dict], Any]]] = [
        ("negative_amount", lambda b: _with(b, requested_amount=-5000)),
        ("zero_amount", lambda b: _with(b, requested_amount=0)),
        ("amount_as_string", lambda b: _with(b, requested_amount="100000")),
        ("unsupported_currency", lambda b: _with(b, currency="USD")),
        ("unknown_product", lambda b: _with(b, product="CRYPTO_BRIDGE_LOAN")),
        ("missing_case_id", lambda b: _with(b, case_id=_DROP)),
        ("missing_amount", lambda b: _with(b, requested_amount=_DROP)),
        ("bureau_score_out_of_range", lambda b: _with(b, bureau__score=1200)),
        ("unknown_fraud_status", lambda b: _with(b, fraud__status="SUPER_HIGH")),
        ("negative_cash", lambda b: _with(b, monthly_cash_available=-1)),
        ("amount_above_product_limit", lambda b: _with(b, requested_amount=9_000_000)),
        ("body_not_an_object", lambda b: [b]),
    ]
    return [_case("INVALID_INPUT", fn(base), expected={"type": "REJECT"}, tags=[name], keep_input=True)
            for name, fn in variants]


def gen_property(base: dict, cfg: dict, seed: int) -> list[dict]:
    """Version-regression cases from Hypothesis with a fixed seed (spec §5.1 Property testing)."""
    from hypothesis import HealthCheck, Phase, given, settings
    from hypothesis import seed as hseed
    from hypothesis import strategies as st

    count = int(cfg.get("count", 1000))
    collected: list[dict] = []

    applicant = st.fixed_dictionaries({
        "product": st.sampled_from(["SME_WORKING_CAPITAL", "SME_TERM_LOAN"]),
        "requested_amount": st.integers(10_000, 2_000_000).map(lambda x: x - x % 1000),
        "monthly_debt_service": st.integers(1_000, 40_000).map(lambda x: x - x % 50),
        "dscr_x100": st.integers(60, 400),
        "score": st.integers(450, 850),
        "fraud": st.sampled_from(["CLEAR"] * 12 + ["MEDIUM", "HIGH"]),
        "alert_resolved": st.booleans(),
        "identity": st.sampled_from(["VERIFIED"] * 10 + ["PENDING", "FAILED"]),
    })

    @hseed(seed)
    @settings(max_examples=count, database=None, phases=[Phase.generate], deadline=None,
              suppress_health_check=list(HealthCheck))
    @given(applicant)
    def _collect(a: dict) -> None:
        collected.append(a)

    _collect()
    cases, seen = [], set()
    for a in collected:
        fin = _financials(a["dscr_x100"] / 100, a["monthly_debt_service"])
        fraud: dict[str, Any] = {"status": a["fraud"]}
        if a["fraud"] == "HIGH" and a["alert_resolved"]:
            fraud["alert_resolved"] = True
        inp = _with(base, product=a["product"], requested_amount=a["requested_amount"],
                    bureau__score=a["score"], fraud=fraud, identity={"status": a["identity"]}, **fin)
        h = content_hash(inp)
        if h in seen:
            continue
        seen.add(h)
        cases.append(_case("VERSION_REGRESSION", inp, tags=["hypothesis"]))
    return cases[:count]


def gen_historical(rows: list[dict], cfg: dict) -> list[dict]:
    return [_case("HISTORICAL_REPLAY", r, tags=["historical"], keep_case_id=True) for r in rows]


def gen_fixtures(cfg: dict) -> list[dict]:
    out = []
    for c in cfg.get("cases", []):
        cat = c.get("category", "NORMAL")
        out.append(_case(cat, c["input"], scenario=default_scenario(**(c.get("scenario") or {})),
                         expected=c.get("expected_behavior"), tags=c.get("tags", []) + ["fixture"],
                         keep_case_id="case_id" in (c["input"] if isinstance(c["input"], dict) else {}),
                         case_key=c.get("case_key")))
    return out


def _case(category: str, inp: Any, scenario: dict | None = None, expected: dict | None = None,
          tags: list[str] | None = None, keep_input: bool = False, keep_case_id: bool = False,
          case_key: str | None = None) -> dict:
    return {"category": category, "input": inp, "scenario": scenario or default_scenario(),
            "expected_behavior": expected or {}, "tags": tags or [], "_keep_input": keep_input,
            "_keep_case_id": keep_case_id, "case_key": case_key}


PREFIX = {"NORMAL": "NRM", "BOUNDARY": "BND", "MISSING_DATA": "MIS", "DEPENDENCY_FAILURE": "DEP",
          "CROSS_SYSTEM_CONFLICT": "CSC", "VERSION_REGRESSION": "REG", "DUPLICATE": "DUP",
          "INVALID_INPUT": "INV", "HISTORICAL_REPLAY": "HIS"}

KNOWN_GENERATORS = {"normal", "boundary", "missing_data", "dependency_failure", "cross_system_conflict",
                    "duplicate", "invalid_input", "property", "fixtures", "historical"}


def generate_suite(config: dict[str, Any], policy_sets: list[CompiledPolicySet],
                   historical_rows: list[dict] | None = None) -> list[dict[str, Any]]:
    """Build the full, ordered, keyed case list for a suite configuration."""
    seed = int(config.get("seed", 20261124))
    base = {**DEFAULT_BASE_CASE, **(config.get("base_case") or {})}
    cases: list[dict] = []
    for g in config.get("generators", []):
        kind = g.get("type")
        if kind not in KNOWN_GENERATORS:
            raise GenerationError(f"unknown generator '{kind}'. Known: {sorted(KNOWN_GENERATORS)}")
        if kind == "normal":
            cases += gen_normal(base, g)
        elif kind == "boundary":
            cases += gen_boundary(base, g, policy_sets)
        elif kind == "missing_data":
            cases += gen_missing_data(base, g)
        elif kind == "dependency_failure":
            cases += gen_dependency_failure(base, g)
        elif kind == "cross_system_conflict":
            cases += gen_cross_system_conflict(base, g)
        elif kind == "duplicate":
            cases += gen_duplicate(base, g)
        elif kind == "invalid_input":
            cases += gen_invalid_input(base, g)
        elif kind == "property":
            cases += gen_property(base, g, seed)
        elif kind == "fixtures":
            cases += gen_fixtures(g)
        elif kind == "historical":
            cases += gen_historical(historical_rows or [], g)

    # Stable keys: per-category sequence, plus a short suite tag so application ids are unique
    # across suites (the dependency simulator keys injected faults by application id).
    tag = content_hash({"config": config, "n": len(cases)})[:6].upper()
    counters: dict[str, int] = {}
    keyed: list[dict] = []
    used: set[str] = set()
    for c in cases:
        cat = c["category"]
        counters[cat] = counters.get(cat, 0) + 1
        key = c.get("case_key") or f"{PREFIX.get(cat, 'CAS')}-{counters[cat]:05d}"
        if not isinstance(key, str) or not CASE_KEY_RE.fullmatch(key):
            # case keys become part of evidence storage paths: allow a safe character set only
            raise GenerationError(f"invalid case_key {key!r}: use 1-100 letters, digits, '.', '_' or '-'")
        if key in used:
            raise GenerationError(f"duplicate case key {key}")
        used.add(key)
        inp = c["input"]
        if isinstance(inp, dict) and not c["_keep_case_id"] and not (c["_keep_input"] and "case_id" not in inp and cat == "INVALID_INPUT" and "missing_case_id" in c["tags"]):
            inp = {"case_id": f"{tag}-{key}", **{k: v for k, v in inp.items() if k != "case_id"}}
        elif isinstance(inp, list):
            inp = [{"case_id": f"{tag}-{key}", **x} if isinstance(x, dict) else x for x in inp]
        keyed.append({
            "case_key": key, "category": cat, "input": inp, "scenario": c["scenario"],
            "expected_behavior": c["expected_behavior"], "tags": c["tags"],
        })
    return keyed


def application_id(case: dict[str, Any]) -> str | None:
    inp = case.get("input")
    if isinstance(inp, dict):
        return inp.get("case_id")
    if isinstance(inp, list) and inp and isinstance(inp[0], dict):
        return inp[0].get("case_id")
    return None
