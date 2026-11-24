"""Policy templates: ready-made starting points that cut setup from days to hours.

Every threshold in a template is left empty. The institution supplies its own values when it
instantiates the template; ModelAssure never chooses credit or regulatory thresholds."""
from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

TEMPLATE_DIR = Path(__file__).parent / "templates"


class TemplateError(ValueError):
    pass


@lru_cache
def load_templates() -> dict[str, dict[str, Any]]:
    return {t["template_id"]: t for t in (json.loads(p.read_text(encoding="utf-8")) for p in sorted(TEMPLATE_DIR.glob("*.json")))}


def instantiate(template_id: str, parameters: dict[str, Any], label: str | None = None) -> dict[str, Any]:
    t = load_templates().get(template_id)
    if t is None:
        raise TemplateError(f"unknown template '{template_id}'")
    content = copy.deepcopy(t["content"])
    declared = {p["name"]: p for p in t["parameters"]}
    unknown = set(parameters) - set(declared)
    if unknown:
        raise TemplateError(f"unknown parameter(s) {sorted(unknown)}; this template takes {sorted(declared)}")
    for name, spec in declared.items():
        v = parameters.get(name)
        if v is None:
            continue  # left empty: validation will report PARAMETER_NOT_SET until supplied
        if spec["type"] == "number" and (isinstance(v, bool) or not isinstance(v, (int, float))):
            raise TemplateError(f"parameter '{name}' must be a number")
        content["parameters"][name] = v
    if label:
        content["label"] = label
    content["source_template"] = {"template_id": t["template_id"], "template_hash": template_hash(t)}
    return content


def template_hash(t: dict[str, Any]) -> str:
    from common.hashing import content_hash
    return content_hash(t)


def summary(t: dict[str, Any]) -> dict[str, Any]:
    c = t["content"]
    return {k: t.get(k) for k in ("template_id", "name", "product", "summary", "legal_note", "generator_profile",
                                  "parameters", "required_input_fields")} | {
        "counts": {"policies": len(c.get("policies") or []), "change_controls": len(c.get("change_controls") or [])},
        "policies": [{"policy_id": p["policy_id"], "description": p["description"], "severity": p["severity"]}
                     for p in c.get("policies") or []],
        "template_hash": template_hash(t)}
