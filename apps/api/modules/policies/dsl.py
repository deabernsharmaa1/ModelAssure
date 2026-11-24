"""Structured financial policy language (Module C).

Design rules (spec §7.3):
  * Only a closed set of operators is supported. Nothing a user supplies is ever executed as code.
  * Policies are data. They are parsed into an immutable node tree, validated, then evaluated.

Evaluation uses three-valued (Kleene) logic: TRUE, FALSE, UNKNOWN (None). UNKNOWN arises when a
field a condition needs is missing or has the wrong type. It is how the engine tells "the rule did
not apply" apart from "we could not tell whether the rule applied" (→ INCONCLUSIVE, never PASS).

Field references
  A field is a dotted path, optionally prefixed with a namespace:
    input.*     the canonical decision request           (default namespace for `when`)
    output.*    the normalised candidate response        (default namespace for `assert`)
    baseline.*  the normalised baseline response
    scenario.*  scenario metadata, e.g. scenario.faults.bureau = "TIMEOUT"
    derived.*   declarative derived fields, e.g. derived.dscr
    params.*    customer-supplied policy parameters, e.g. params.min_dscr
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

# --------------------------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------------------------
DECISIONS: tuple[str, ...] = ("APPROVE", "DECLINE", "REVIEW", "HOLD", "REFER")
SEVERITIES: tuple[str, ...] = ("CRITICAL", "HIGH", "MEDIUM", "LOW")
NAMESPACES: tuple[str, ...] = ("input", "output", "baseline", "scenario", "derived", "params")

COMPARISON_OPS = {"eq", "ne", "gt", "gte", "lt", "lte"}
NUMERIC_OPS = {"gt", "gte", "lt", "lte"}
SET_OPS = {"in", "not_in"}
PRESENCE_OPS = {"exists", "not_exists"}
LEAF_OPS = COMPARISON_OPS | SET_OPS | PRESENCE_OPS | {"contains"}
GROUP_OPS = {"all", "any"}
DERIVED_OPS = {"add", "sub", "mul", "div", "min", "max", "abs", "neg", "coalesce", "round"}

MISSING = object()  # sentinel for "field absent"


class PolicyError(ValueError):
    """Raised for structurally or semantically invalid policy definitions."""

    def __init__(self, message: str, path: str = ""):
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


# --------------------------------------------------------------------------------------------
# Node tree
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Leaf:
    namespace: str
    path: tuple[str, ...]
    operator: str
    value: Any = None
    value_ref: tuple[str, tuple[str, ...]] | None = None  # (namespace, path)
    if_missing: bool | None = None  # None → UNKNOWN (default); True/False → explicit result

    @property
    def ref(self) -> str:
        return ".".join((self.namespace, *self.path))


@dataclass(frozen=True)
class Group:
    operator: str  # "all" | "any"
    children: tuple["Node", ...]


@dataclass(frozen=True)
class Not:
    child: "Node"


@dataclass(frozen=True)
class Always:
    """A missing `when` clause means the rule always applies."""


Node = Leaf | Group | Not | Always


def split_ref(ref: str, default_ns: str, where: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(ref, str) or not ref.strip():
        raise PolicyError("field must be a non-empty string", where)
    parts = tuple(p for p in ref.strip().split("."))
    if any(p == "" for p in parts):
        raise PolicyError(f"invalid field path '{ref}'", where)
    if parts[0] in NAMESPACES:
        if len(parts) == 1:
            raise PolicyError(f"field '{ref}' names a namespace but no field", where)
        return parts[0], parts[1:]
    return default_ns, parts


def parse_condition(obj: Any, default_ns: str, where: str = "condition") -> Node:
    """Parse a JSON condition into a node tree. Accepts:
         {"field":..,"operator":..,"value":..}           leaf
         {"all":[...]} / {"any":[...]} / {"not": {...}}  groups
         {"operator":"all"|"any","conditions":[...]}     group, operator form
    """
    if obj is None:
        return Always()
    if not isinstance(obj, dict):
        raise PolicyError("condition must be an object", where)

    for key in ("all", "any"):
        if key in obj:
            extra = set(obj) - {key}
            if extra:
                raise PolicyError(f"unexpected keys {sorted(extra)} next to '{key}'", where)
            return _group(key, obj[key], default_ns, where)
    if "not" in obj:
        if set(obj) != {"not"}:
            raise PolicyError("'not' must be the only key", where)
        return Not(parse_condition(obj["not"], default_ns, f"{where}.not"))

    op = obj.get("operator")
    if op in GROUP_OPS:
        if "conditions" not in obj:
            raise PolicyError(f"operator '{op}' needs 'conditions'", where)
        return _group(op, obj["conditions"], default_ns, where)

    allowed = {"field", "operator", "value", "value_ref", "if_missing"}
    extra = set(obj) - allowed
    if extra:
        raise PolicyError(f"unsupported keys {sorted(extra)}", where)
    if op not in LEAF_OPS:
        raise PolicyError(
            f"unsupported operator '{op}'. Allowed: {sorted(LEAF_OPS | GROUP_OPS)}", where
        )
    ns, path = split_ref(obj.get("field"), default_ns, where)

    has_value = "value" in obj
    has_ref = "value_ref" in obj and obj["value_ref"] is not None
    if op in PRESENCE_OPS:
        if has_value or has_ref:
            raise PolicyError(f"operator '{op}' takes no value", where)
    else:
        if has_value == has_ref:
            raise PolicyError(f"operator '{op}' needs exactly one of 'value' or 'value_ref'", where)

    value = obj.get("value")
    value_ref = None
    if has_ref:
        value_ref = split_ref(obj["value_ref"], "params", f"{where}.value_ref")
    if has_value:
        if op in NUMERIC_OPS and not _is_number(value):
            raise PolicyError(f"operator '{op}' needs a numeric value, got {value!r}", where)
        if op in SET_OPS and not (isinstance(value, list) and value):
            raise PolicyError(f"operator '{op}' needs a non-empty list value", where)
        if op not in SET_OPS and isinstance(value, (list, dict)) and op != "contains":
            raise PolicyError(f"operator '{op}' does not accept a list/object value", where)

    if_missing = obj.get("if_missing")
    if if_missing not in (None, "unknown", "true", "false", True, False):
        raise PolicyError("if_missing must be 'unknown', 'true' or 'false'", where)
    im: bool | None = {None: None, "unknown": None, "true": True, "false": False, True: True, False: False}[if_missing]
    if op in PRESENCE_OPS and im is not None:
        raise PolicyError("if_missing is meaningless for exists/not_exists", where)

    return Leaf(ns, path, op, value, value_ref, im)


def _group(op: str, items: Any, default_ns: str, where: str) -> Group:
    if not isinstance(items, list) or not items:
        raise PolicyError(f"'{op}' needs a non-empty list", where)
    return Group(op, tuple(parse_condition(c, default_ns, f"{where}.{op}[{i}]") for i, c in enumerate(items)))


def iter_leaves(node: Node) -> Iterable[Leaf]:
    if isinstance(node, Leaf):
        yield node
    elif isinstance(node, Group):
        for c in node.children:
            yield from iter_leaves(c)
    elif isinstance(node, Not):
        yield from iter_leaves(node.child)


def node_to_text(node: Node) -> str:
    if isinstance(node, Always):
        return "always"
    if isinstance(node, Not):
        return f"not ({node_to_text(node.child)})"
    if isinstance(node, Group):
        joiner = " and " if node.operator == "all" else " or "
        return "(" + joiner.join(node_to_text(c) for c in node.children) + ")"
    rhs = ""
    if node.operator not in PRESENCE_OPS:
        rhs = " " + (".".join((node.value_ref[0], *node.value_ref[1])) if node.value_ref else repr(node.value))
    return f"{node.ref} {node.operator}{rhs}"


# --------------------------------------------------------------------------------------------
# Field resolution and evaluation
# --------------------------------------------------------------------------------------------
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and math.isnan(v))


def resolve(ctx: dict[str, Any], ns: str, path: tuple[str, ...]) -> Any:
    cur: Any = ctx.get(ns, MISSING)
    for part in path:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return MISSING
    return cur


def eval_node(node: Node, ctx: dict[str, Any]) -> bool | None:
    if isinstance(node, Always):
        return True
    if isinstance(node, Not):
        r = eval_node(node.child, ctx)
        return None if r is None else not r
    if isinstance(node, Group):
        results = [eval_node(c, ctx) for c in node.children]
        if node.operator == "all":
            if any(r is False for r in results):
                return False
            if any(r is None for r in results):
                return None
            return True
        if any(r is True for r in results):
            return True
        if any(r is None for r in results):
            return None
        return False
    return _eval_leaf(node, ctx)


def _eval_leaf(leaf: Leaf, ctx: dict[str, Any]) -> bool | None:
    actual = resolve(ctx, leaf.namespace, leaf.path)
    if leaf.operator == "exists":
        return actual is not MISSING and actual is not None
    if leaf.operator == "not_exists":
        return actual is MISSING or actual is None
    if actual is MISSING:
        return leaf.if_missing  # None → UNKNOWN

    if leaf.value_ref is not None:
        expected = resolve(ctx, *leaf.value_ref)
        if expected is MISSING:
            return None
    else:
        expected = leaf.value

    op = leaf.operator
    if op == "eq":
        return _equal(actual, expected)
    if op == "ne":
        return not _equal(actual, expected)
    if op in NUMERIC_OPS:
        if not (_is_number(actual) and _is_number(expected)):
            return None
        return {"gt": actual > expected, "gte": actual >= expected, "lt": actual < expected, "lte": actual <= expected}[op]
    if op in SET_OPS:
        if not isinstance(expected, list):
            return None
        hit = any(_equal(actual, e) for e in expected)
        return hit if op == "in" else not hit
    if op == "contains":
        if isinstance(actual, list):
            return any(_equal(a, expected) for a in actual)
        if isinstance(actual, str) and isinstance(expected, str):
            return expected in actual
        return None
    return None  # pragma: no cover - parse_condition rejects unknown operators


def _equal(a: Any, b: Any) -> bool:
    if _is_number(a) and _is_number(b):
        return math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-12)
    return a == b


# --------------------------------------------------------------------------------------------
# Derived fields (declarative arithmetic; no code execution)
# --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class DerivedField:
    name: str
    operator: str
    args: tuple[Any, ...]  # literal numbers or (ns, path) refs
    description: str = ""


def parse_derived(defs: list[dict[str, Any]] | None) -> list[DerivedField]:
    out: list[DerivedField] = []
    seen: set[str] = set()
    for i, d in enumerate(defs or []):
        where = f"derived_fields[{i}]"
        if not isinstance(d, dict):
            raise PolicyError("must be an object", where)
        name = d.get("name")
        if not isinstance(name, str) or not name.isidentifier():
            raise PolicyError("name must be an identifier", where)
        if name in seen:
            raise PolicyError(f"duplicate derived field '{name}'", where)
        op = d.get("operator")
        if op not in DERIVED_OPS:
            raise PolicyError(f"unsupported derived operator '{op}'. Allowed: {sorted(DERIVED_OPS)}", where)
        raw_args = d.get("args")
        if not isinstance(raw_args, list) or not raw_args:
            raise PolicyError("args must be a non-empty list", where)
        arity = {"abs": 1, "neg": 1, "sub": 2, "div": 2, "round": 2}
        if op in arity and len(raw_args) != arity[op]:
            raise PolicyError(f"'{op}' takes exactly {arity[op]} args", where)
        args: list[Any] = []
        for j, a in enumerate(raw_args):
            if _is_number(a):
                args.append(a)
            elif isinstance(a, str):
                ns, path = split_ref(a, "input", f"{where}.args[{j}]")
                if ns == "derived" and path[0] not in seen:
                    raise PolicyError(f"derived field '{path[0]}' must be defined before use", where)
                if ns in ("output", "baseline"):
                    raise PolicyError("derived fields may only use input, scenario, params or earlier derived fields", where)
                args.append((ns, path))
            else:
                raise PolicyError(f"arg {a!r} must be a number or a field reference", where)
        seen.add(name)
        out.append(DerivedField(name, op, tuple(args), str(d.get("description", ""))))
    return out


def compute_derived(defs: list[DerivedField], ctx: dict[str, Any]) -> dict[str, Any]:
    derived: dict[str, Any] = {}
    ctx = {**ctx, "derived": derived}
    for d in defs:
        vals = [a if _is_number(a) else resolve(ctx, *a) for a in d.args]
        result: Any = MISSING
        if d.operator == "coalesce":
            result = next((v for v in vals if v is not MISSING and v is not None), MISSING)
        elif all(_is_number(v) for v in vals):
            try:
                if d.operator == "add":
                    result = sum(vals)
                elif d.operator == "sub":
                    result = vals[0] - vals[1]
                elif d.operator == "mul":
                    result = math.prod(vals)
                elif d.operator == "div":
                    result = MISSING if vals[1] == 0 else vals[0] / vals[1]
                elif d.operator == "min":
                    result = min(vals)
                elif d.operator == "max":
                    result = max(vals)
                elif d.operator == "abs":
                    result = abs(vals[0])
                elif d.operator == "neg":
                    result = -vals[0]
                elif d.operator == "round":
                    result = round(vals[0], int(vals[1]))
            except (OverflowError, ValueError):
                result = MISSING
        if result is not MISSING:
            derived[d.name] = result
    return derived


# --------------------------------------------------------------------------------------------
# Allowed-decision analysis (used for precedence and static conflict detection)
# --------------------------------------------------------------------------------------------
ALL_DECISIONS = frozenset(DECISIONS)


def allowed_decisions(node: Node) -> frozenset[str]:
    """Over-approximate the set of normalised decisions an `assert` clause can accept."""
    if isinstance(node, Always):
        return ALL_DECISIONS
    if isinstance(node, Not):
        inner = node.child
        if isinstance(inner, Leaf) and _is_decision_leaf(inner):
            return ALL_DECISIONS - allowed_decisions(inner)
        return ALL_DECISIONS
    if isinstance(node, Group):
        sets = [allowed_decisions(c) for c in node.children]
        if node.operator == "all":
            out = ALL_DECISIONS
            for s in sets:
                out = out & s
            return out
        out: frozenset[str] = frozenset()
        for s in sets:
            out = out | s
        return out
    if not _is_decision_leaf(node) or node.value_ref is not None:
        return ALL_DECISIONS
    if node.operator == "eq":
        return frozenset({node.value}) & ALL_DECISIONS
    if node.operator == "ne":
        return ALL_DECISIONS - {node.value}
    if node.operator == "in":
        return frozenset(node.value) & ALL_DECISIONS
    if node.operator == "not_in":
        return ALL_DECISIONS - set(node.value)
    return ALL_DECISIONS


def _is_decision_leaf(leaf: Leaf) -> bool:
    return leaf.namespace == "output" and leaf.path == ("decision",)


# --------------------------------------------------------------------------------------------
# Mutual exclusivity of `when` clauses (best-effort proof; unproven → potential conflict)
# --------------------------------------------------------------------------------------------
def provably_exclusive(a: Node, b: Node) -> bool:
    if isinstance(a, Group) and a.operator == "any":
        return all(provably_exclusive(c, b) for c in a.children)
    if isinstance(b, Group) and b.operator == "any":
        return all(provably_exclusive(a, c) for c in b.children)
    la, lb = _conjuncts(a), _conjuncts(b)
    return any(_leaves_exclusive(x, y) for x in la for y in lb)


def _conjuncts(n: Node) -> list[Leaf]:
    if isinstance(n, Leaf):
        return [n]
    if isinstance(n, Group) and n.operator == "all":
        out: list[Leaf] = []
        for c in n.children:
            out.extend(_conjuncts(c))
        return out
    return []


def _leaves_exclusive(x: Leaf, y: Leaf) -> bool:
    if (x.namespace, x.path) != (y.namespace, y.path):
        return False
    if x.value_ref is not None or y.value_ref is not None:
        # Two comparisons against the same named parameter, e.g. dscr < p and dscr >= p
        if x.value_ref == y.value_ref and x.value_ref is not None:
            pair = {x.operator, y.operator}
            return pair in ({"lt", "gte"}, {"gt", "lte"}, {"eq", "ne"})
        return False
    if {x.operator, y.operator} == {"exists", "not_exists"}:
        return True
    # If either side matches when missing, a missing field can satisfy both.
    if x.if_missing is True or y.if_missing is True:
        return False
    sx, sy = _value_set(x), _value_set(y)
    if sx is not None and sy is not None:
        return not (sx & sy)
    if sx is not None and y.operator in ("ne", "not_in"):
        return sx <= set(y.value if isinstance(y.value, list) else [y.value])
    if sy is not None and x.operator in ("ne", "not_in"):
        return sy <= set(x.value if isinstance(x.value, list) else [x.value])
    rx, ry = _interval(x), _interval(y)
    if rx and ry:
        (lo1, li1, hi1, hi1i), (lo2, li2, hi2, hi2i) = rx, ry
        lo, lo_inc = (lo1, li1) if lo1 > lo2 else (lo2, li2) if lo2 > lo1 else (lo1, li1 and li2)
        hi, hi_inc = (hi1, hi1i) if hi1 < hi2 else (hi2, hi2i) if hi2 < hi1 else (hi1, hi1i and hi2i)
        return lo > hi or (lo == hi and not (lo_inc and hi_inc))
    return False


def _value_set(x: Leaf) -> set | None:
    if x.operator == "eq":
        return {x.value}
    if x.operator == "in":
        return set(x.value)
    return None


def _interval(x: Leaf):
    inf = float("inf")
    v = x.value
    if not _is_number(v):
        return None
    return {
        "gt": (v, False, inf, False),
        "gte": (v, True, inf, False),
        "lt": (-inf, False, v, False),
        "lte": (-inf, False, v, True),
        "eq": (v, True, v, True),
    }.get(x.operator)


@dataclass
class Issue:
    level: str  # "error" | "warning"
    code: str
    message: str
    refs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"level": self.level, "code": self.code, "message": self.message, "refs": self.refs}
