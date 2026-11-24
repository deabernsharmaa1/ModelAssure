import json
from pathlib import Path

import pytest

from modules.policies.dsl import PolicyError, eval_node, parse_condition
from modules.policies.policy_set import (
    NOT_APPLICABLE, SATISFIED, SUPERSEDED, UNKNOWN, VIOLATED,
    build_context, compile_policy_set, evaluate_policies,
)

FIX = Path(__file__).resolve().parents[2] / "decision_simulator" / "fixtures"


def cond(obj, ns="input"):
    return parse_condition(obj, ns)


class TestOperators:
    ctx = {"input": {"a": 5, "s": "X", "lst": ["p", "q"], "nested": {"b": None}}}

    @pytest.mark.parametrize("op,value,expected", [
        ("eq", 5, True), ("ne", 5, False), ("gt", 4, True), ("gte", 5, True),
        ("lt", 5, False), ("lte", 5, True), ("in", [1, 5], True), ("not_in", [5], False),
    ])
    def test_leaf_operators(self, op, value, expected):
        assert eval_node(cond({"field": "a", "operator": op, "value": value}), self.ctx) is expected

    def test_contains(self):
        assert eval_node(cond({"field": "lst", "operator": "contains", "value": "q"}), self.ctx) is True

    def test_missing_field_is_unknown_not_false(self):
        assert eval_node(cond({"field": "nope", "operator": "eq", "value": 1}), self.ctx) is None

    def test_if_missing_override(self):
        assert eval_node(cond({"field": "nope", "operator": "eq", "value": 1, "if_missing": "false"}), self.ctx) is False

    def test_exists(self):
        assert eval_node(cond({"field": "a", "operator": "exists"}), self.ctx) is True
        assert eval_node(cond({"field": "nested.b", "operator": "exists"}), self.ctx) is False
        assert eval_node(cond({"field": "zzz", "operator": "not_exists"}), self.ctx) is True

    def test_type_mismatch_is_unknown(self):
        assert eval_node(cond({"field": "s", "operator": "gt", "value": 3}), self.ctx) is None

    def test_kleene_logic(self):
        unknown = {"field": "nope", "operator": "eq", "value": 1}
        t = {"field": "a", "operator": "eq", "value": 5}
        f = {"field": "a", "operator": "eq", "value": 6}
        assert eval_node(cond({"all": [unknown, f]}), self.ctx) is False
        assert eval_node(cond({"all": [unknown, t]}), self.ctx) is None
        assert eval_node(cond({"any": [unknown, t]}), self.ctx) is True
        assert eval_node(cond({"any": [unknown, f]}), self.ctx) is None
        assert eval_node(cond({"not": unknown}), self.ctx) is None

    def test_operator_form_groups(self):
        node = cond({"operator": "any", "conditions": [{"field": "a", "operator": "eq", "value": 6},
                                                       {"field": "a", "operator": "eq", "value": 5}]})
        assert eval_node(node, self.ctx) is True


class TestRejectsUnsafeOrInvalid:
    @pytest.mark.parametrize("bad", [
        {"field": "a", "operator": "__import__('os').system('id')", "value": 1},
        {"field": "a", "operator": "eval", "value": "1+1"},
        {"field": "a", "operator": "gt", "value": "ten"},
        {"field": "a", "operator": "in", "value": []},
        {"field": "a", "operator": "eq"},
        {"field": "a", "operator": "exists", "value": 1},
        {"field": "", "operator": "eq", "value": 1},
        {"field": "a", "operator": "eq", "value": 1, "code": "x"},
        {"all": []},
    ])
    def test_invalid_conditions_raise(self, bad):
        with pytest.raises(PolicyError):
            cond(bad)

    def test_unknown_decision_value_rejected(self):
        doc = {"policies": [{"policy_id": "P", "assert": {"field": "decision", "operator": "eq", "value": "APPROVED"}}]}
        with pytest.raises(PolicyError, match="unknown decision"):
            compile_policy_set(doc)

    def test_duplicate_policy_ids_rejected(self):
        p = {"policy_id": "P", "assert": {"field": "decision", "operator": "eq", "value": "REVIEW"}}
        with pytest.raises(PolicyError, match="duplicate"):
            compile_policy_set({"policies": [p, p]})

    def test_undefined_parameter_is_reported(self):
        doc = {"policies": [{"policy_id": "P", "when": {"field": "x", "operator": "lt", "value_ref": "params.nope"},
                             "assert": {"field": "decision", "operator": "eq", "value": "REVIEW"}}]}
        cps = compile_policy_set(doc)
        assert [i.code for i in cps.errors()] == ["UNKNOWN_PARAMETER"]


class TestDerivedFields:
    doc = {
        "parameters": {"min": 1.5},
        "derived_fields": [{"name": "dscr", "operator": "div", "args": ["cash", "debt"]}],
        "policies": [{"policy_id": "P", "when": {"field": "derived.dscr", "operator": "lt", "value_ref": "params.min"},
                      "assert": {"field": "decision", "operator": "in", "value": ["REFER", "DECLINE"]}}],
    }

    def outcome(self, inp, decision):
        cps = compile_policy_set(self.doc)
        return evaluate_policies(cps, build_context(cps, inp, {}, {"decision": decision}, None))[0].status

    def test_ratio_below_limit_violated(self):
        assert self.outcome({"cash": 14000, "debt": 10000}, "APPROVE") == VIOLATED

    def test_ratio_at_limit_not_applicable(self):
        assert self.outcome({"cash": 15000, "debt": 10000}, "APPROVE") == NOT_APPLICABLE

    def test_division_by_zero_is_unknown(self):
        assert self.outcome({"cash": 1, "debt": 0}, "APPROVE") == UNKNOWN

    def test_forward_reference_rejected(self):
        with pytest.raises(PolicyError):
            compile_policy_set({**self.doc, "derived_fields": [{"name": "x", "operator": "abs", "args": ["derived.y"]}]})


class TestConflicts:
    def _doc(self, prec_a, prec_b, when_b):
        return {"policies": [
            {"policy_id": "A", "precedence": prec_a, "when": {"field": "s", "operator": "eq", "value": "X"},
             "assert": {"field": "decision", "operator": "eq", "value": "REVIEW"}},
            {"policy_id": "B", "precedence": prec_b, "when": when_b,
             "assert": {"field": "decision", "operator": "eq", "value": "HOLD"}},
        ]}

    def test_equal_precedence_overlapping_is_error(self):
        cps = compile_policy_set(self._doc(0, 0, {"field": "f", "operator": "eq", "value": "HIGH"}))
        assert [i.code for i in cps.errors()] == ["POLICY_CONFLICT"]

    def test_mutually_exclusive_conditions_are_fine(self):
        cps = compile_policy_set(self._doc(0, 0, {"field": "s", "operator": "eq", "value": "Y"}))
        assert cps.errors() == []

    def test_exclusive_numeric_ranges(self):
        doc = {"policies": [
            {"policy_id": "A", "when": {"field": "x", "operator": "lt", "value": 1},
             "assert": {"field": "decision", "operator": "eq", "value": "DECLINE"}},
            {"policy_id": "B", "when": {"field": "x", "operator": "gte", "value": 1},
             "assert": {"field": "decision", "operator": "eq", "value": "APPROVE"}},
        ]}
        assert compile_policy_set(doc).errors() == []

    def test_precedence_supersedes_at_runtime(self):
        cps = compile_policy_set(self._doc(10, 20, {"field": "f", "operator": "eq", "value": "HIGH"}))
        assert cps.errors() == []
        out = {o.policy_id: o.status for o in evaluate_policies(
            cps, build_context(cps, {"s": "X", "f": "HIGH"}, {}, {"decision": "HOLD"}, None))}
        assert out == {"A": SUPERSEDED, "B": SATISFIED}


def test_demo_policy_sets_compile_without_errors():
    for name in ("policy_set_2026-06.json", "policy_set_2026-11.json"):
        cps = compile_policy_set(json.loads((FIX / name).read_text()))
        assert cps.errors() == [], name


def test_spec_example_policy_pol_001():
    """Spec §7.3/§7.4: bureau UNAVAILABLE + candidate APPROVE → POL-001 violated."""
    doc = {"policies": [{
        "policy_id": "POL-001", "version": 1, "description": "Missing bureau data requires review",
        "when": {"field": "bureau.status", "operator": "eq", "value": "UNAVAILABLE"},
        "assert": {"field": "decision", "operator": "eq", "value": "REVIEW"},
        "severity": "CRITICAL", "enabled": True}]}
    cps = compile_policy_set(doc)
    inp = {"case_id": "CASE-1001", "bureau": {"status": "UNAVAILABLE"}}
    [o] = evaluate_policies(cps, build_context(cps, inp, {}, {"decision": "APPROVE"}, None))
    assert (o.status, o.expected, o.observed) == (VIOLATED, "REVIEW", "APPROVE")


# ---- property tests of the policy engine (Hypothesis) ------------------------------------------
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from modules.policies.dsl import eval_node, parse_condition  # noqa: E402

_numbers = st.one_of(st.integers(-10**9, 10**9), st.floats(allow_nan=False, allow_infinity=False, width=64))


@settings(max_examples=300, deadline=None)
@given(x=_numbers, t=_numbers)
def test_comparisons_are_complementary(x, t):
    """For any present numeric value exactly one of gt / lte (and lt / gte) holds."""
    ctx = {"input": {"v": x}}
    for a, b in (("gt", "lte"), ("lt", "gte")):
        ra = eval_node(parse_condition({"field": "v", "operator": a, "value": t}, "input"), ctx)
        rb = eval_node(parse_condition({"field": "v", "operator": b, "value": t}, "input"), ctx)
        assert {ra, rb} == {True, False}


@settings(max_examples=200, deadline=None)
@given(t=_numbers, op=st.sampled_from(["eq", "ne", "gt", "gte", "lt", "lte"]))
def test_missing_field_is_unknown_never_true(t, op):
    """Three-valued logic: a comparison on an absent field is UNKNOWN, never silently TRUE/FALSE."""
    assert eval_node(parse_condition({"field": "v", "operator": op, "value": t}, "input"), {"input": {}}) is None
