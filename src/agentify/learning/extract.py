"""Compile a verified exploration trace into a parameterized Workflow (docs/SKILL_LIFECYCLE.md Â§4).

Deterministic and inspectable: the host agent names the inputs and binds them to trace steps;
the runtime never guesses parameters.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import ValidationError

from agentify.adapters.base import Action, RiskLevel, Selector
from agentify.errors import LearnError
from agentify.workflows.loader import parse_workflow
from agentify.workflows.models import (
    STEP_ACTIONS,
    Expectation,
    InputSpec,
    OutputSpec,
    Provenance,
    SelectorSpec,
    Step,
    Workflow,
)
from agentify.workflows.template import template_refs

_STEP_FIELDS = (
    "pattern", "value", "keys", "via", "verbatim", "button", "double",
    "property_name", "contains", "gone", "timeout_ms", "idempotent",
)  # fmt: skip


def slugify_skill_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    slug = slug if slug and slug[0].isalpha() else f"skill-{slug}"
    return slug[:48].rstrip("-") or "skill"


def _token_re(literal: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w]){re.escape(literal)}(?![\w])")


def _replace(text: str | None, literal: str, param: str) -> tuple[str | None, bool]:
    if text is None:
        return None, False
    new, n = _token_re(literal).subn("{{" + param + "}}", text)
    return new, n > 0


def _clean_host_selector(sel: Selector) -> Selector | None:
    """Drop session-scoped slugs; keep only durable identity."""
    if sel.automation_id or sel.name:
        return sel.model_copy(update={"slug": None})
    return None


def _step_selector(entry: dict[str, Any], *, bound: bool) -> SelectorSpec | None:
    raw = entry["action"].get("selector")
    if raw is None:
        return None
    host = Selector.model_validate(raw)
    element = entry.get("element") or {}
    role, name = element.get("role"), element.get("name")
    stable_id = element.get("automation_id") if element.get("stable_automation_id") else None
    role_name = Selector(role=role, name=name, exact=True) if name else None

    if bound and role_name is not None:
        # The literal lives in the element name (e.g. a list item): identify it by role + name only.
        return SelectorSpec(role=role, name=name, exact=True)
    if stable_id:
        return SelectorSpec(automation_id=stable_id, fallback=role_name)
    if role_name is not None:
        cleaned = _clean_host_selector(host)
        fb = cleaned if cleaned is not None and cleaned != role_name else None
        return SelectorSpec(**role_name.model_dump(exclude_none=True), fallback=fb)
    cleaned = _clean_host_selector(host)
    if cleaned is None and host.role and host.label:
        # Unnamed control identified by its visible label.
        return SelectorSpec(role=host.role, label=host.label, index=host.index, root=host.root)
    if cleaned is None and host.role and host.index is not None:
        # Unnamed control (e.g. a dropdown labelled only by nearby text): positional identity.
        return SelectorSpec(role=host.role, index=host.index, root=host.root)
    if cleaned is None:
        raise LearnError(
            f"step {entry['index']}: element has no durable identity (no stable automation id or name)",
            details={"element": element},
        )
    return SelectorSpec(**cleaned.model_dump(exclude_none=True))


def _risk_of(steps: list[Step]) -> RiskLevel:
    risk = RiskLevel.READ_ONLY
    for step in steps:
        r = Action(
            type=step.action, selector=Selector(name="x"), value="x", keys="x"
        ).effective_risk
        if r.rank > risk.rank:
            risk = r
    return risk


def extract_workflow(
    *,
    execution: dict[str, Any],
    skill_id: str,
    app_key: str,
    name: str,
    description: str,
    inputs: dict[str, dict[str, Any]],
    bindings: dict[str, list[int]],
    version: int = 1,
    parent_version: int | None = None,
    keep_steps: list[int] | None = None,
    risk: RiskLevel | None = None,
    notes: str = "",
    outputs: dict[str, Any] | None = None,
    optional_steps: list[int] | None = None,
) -> tuple[Workflow, dict[str, str]]:
    """Return the workflow and the parameter values the learning run used."""
    trace: list[dict[str, Any]] = execution["trace"]
    try:
        specs = {k: InputSpec.model_validate(v) for k, v in inputs.items()}
    except ValidationError as exc:
        raise LearnError(f"invalid input spec: {exc}") from exc
    unknown_bound = set(bindings) - set(specs)
    if unknown_bound:
        raise LearnError(f"bindings reference undeclared inputs: {sorted(unknown_bound)}")
    unbound = sorted(p for p in specs if not bindings.get(p))
    if unbound:
        # An input that only appears in the verification would check a value no step sets.
        raise LearnError(f"inputs not bound to any step: {unbound} (add bindings)")

    candidates = [e for e in trace if e.get("ok") and e["action"]["type"] in STEP_ACTIONS]
    if keep_steps is not None:
        by_index = {e["index"]: e for e in trace}
        missing = [i for i in keep_steps if i not in by_index]
        if missing:
            raise LearnError(f"keep_steps reference unknown trace indexes: {missing}")
        bad = [i for i in keep_steps if by_index[i] not in candidates]
        if bad:
            raise LearnError(f"keep_steps include failed or observation-only actions: {bad}")
        candidates = [by_index[i] for i in sorted(keep_steps)]
    if not candidates:
        raise LearnError("the execution has no successful UI actions to learn from")
    kept_indexes = {e["index"] for e in candidates}
    bad_optional = sorted(set(optional_steps or []) - kept_indexes)
    if bad_optional:
        raise LearnError(f"optional_steps must be kept, successful actions: {bad_optional}")

    bound_steps: dict[int, list[str]] = {}
    for param, idxs in bindings.items():
        for i in idxs:
            if i not in kept_indexes:
                raise LearnError(f"binding {param!r} -> step {i}: not a kept, successful action")
            bound_steps.setdefault(i, []).append(param)

    # Literal per input: its example (stringified) if given, else the bound step's value/keys/name.
    literals: dict[str, str] = {}
    for param, spec in specs.items():
        if spec.example is not None:
            example = (
                str(spec.example).lower() if isinstance(spec.example, bool) else str(spec.example)
            )
            literals[param] = (spec.value_map or {}).get(example, example)
            continue
        first = bindings.get(param)
        if not first:
            raise LearnError(f"input {param!r} has no example and no binding")
        entry = next(e for e in candidates if e["index"] == first[0])
        act = entry["action"]
        literal = act.get("value") or act.get("keys") or (act.get("selector") or {}).get("name")
        if not literal:
            raise LearnError(
                f"input {param!r}: step {first[0]} has no value, keys, or selector name to bind"
            )
        literals[param] = str(literal)

    steps: list[Step] = []
    for n, entry in enumerate(candidates, start=1):
        act = entry["action"]
        params_here = bound_steps.get(entry["index"], [])
        fields: dict[str, Any] = {k: act[k] for k in _STEP_FIELDS if k in act}
        # A literal found in value/keys is bound there and the selector keeps its stable identity;
        # otherwise the literal must be in the element name (e.g. a list item) and the selector is templated.
        in_payload = {
            p: any(
                _token_re(literals[p]).search(str(fields.get(k) or "")) for k in ("value", "keys")
            )
            for p in params_here
        }
        selector = _step_selector(
            entry, bound=not all(in_payload.values()) if params_here else False
        )
        for param in params_here:
            hit = False
            for key in ("value", "keys"):
                if key in fields:
                    fields[key], h = _replace(fields[key], literals[param], param)
                    hit |= h
            if not in_payload[param] and selector is not None:
                new_name, h = _replace(selector.name, literals[param], param)
                hit |= h
                fb = selector.fallback
                if fb is not None and fb.name:
                    fb_name, h2 = _replace(fb.name, literals[param], param)
                    fb = fb.model_copy(update={"name": fb_name})
                    hit |= h2
                selector = selector.model_copy(update={"name": new_name, "fallback": fb})
            if not hit:
                raise LearnError(
                    f"binding {param!r}: literal {literals[param]!r} not found in step {entry['index']}",
                    details={"action": act},
                )
        if entry["index"] in set(optional_steps or []):
            # Happens only in some cases (e.g. a confirmation dialog): skipped when it cannot run.
            fields["optional"] = True
        steps.append(Step(id=f"s{n}", action=act["type"], selector=selector, **fields))

    last_kept = max(kept_indexes)
    passed = [v for v in execution["verifications"] if v["result"]["passed"]]
    final_checks = [v for v in passed if v["after_step"] > last_kept]

    def durable(raw: dict[str, Any], what: str) -> dict[str, Any]:
        host_sel = Selector.model_validate({k: val for k, val in raw.items() if k != "fallback"})
        cleaned = _clean_host_selector(host_sel)
        if cleaned is None and host_sel.role and (host_sel.label or host_sel.index is not None):
            cleaned = Selector(
                role=host_sel.role, label=host_sel.label, index=host_sel.index, root=host_sel.root
            )
        if cleaned is None:
            raise LearnError(f"{what} selector has no durable identity (slug only)")
        return cleaned.model_dump(exclude_none=True, exclude_defaults=True)

    def compile_check(v: dict[str, Any]) -> Expectation:
        data = dict(v["expectation"])
        if data.get("selector"):
            data["selector"] = durable(data["selector"], "verification")
        for param in specs:
            data["expected"], _ = _replace(data.get("expected"), literals[param], param)
            if data.get("selector", {}).get("name"):
                data["selector"]["name"], _ = _replace(
                    data["selector"]["name"], literals[param], param
                )
        return Expectation.model_validate(data)

    verification: list[Expectation] = []
    seen: set[str] = set()
    for v in final_checks:
        exp = compile_check(v)
        key = exp.model_dump_json()
        if key not in seen:
            seen.add(key)
            verification.append(exp)

    # Checks verified mid-way (before later kept steps, e.g. navigating back) become step checks
    # on the last kept step before them, but only if they depend on an input: a constant check
    # at that point could describe the start state rather than the result.
    step_by_index = {e["index"]: s for e, s in zip(candidates, steps, strict=True)}
    for v in passed:
        if v["after_step"] > last_kept:
            continue
        before = [i for i in step_by_index if i < v["after_step"]]
        if not before:
            continue
        exp = compile_check(v)
        refs = template_refs(exp.expected) | template_refs(
            exp.selector.name if exp.selector else None
        )
        if not refs:
            continue
        step = step_by_index[max(before)]
        if exp not in step.checks:
            step.checks.append(exp)
    if not verification and not any(s.checks for s in steps):
        raise LearnError(
            "no passed verification after the last kept action: call verify_outcome on the final state first"
        )

    # Outputs: values the workflow returns (e.g. a translation the app shows). Each is either a
    # trace index of a successful read (read_value) or an explicit {selector, property}.
    output_specs: dict[str, OutputSpec] = {}
    by_idx = {e["index"]: e for e in trace}
    for oname, raw_out in (outputs or {}).items():
        if isinstance(raw_out, int):
            read_entry = by_idx.get(raw_out)
            if (
                read_entry is None
                or not read_entry.get("ok")
                or not read_entry["action"].get("selector")
            ):
                raise LearnError(f"output {oname!r}: step {raw_out} is not a successful read")
            prop = (
                "value"
                if read_entry["action"]["type"] == "read_value" and read_entry.get("value")
                else "name"
            )
            data_out: dict[str, Any] = {
                "selector": read_entry["action"]["selector"],
                "property": prop,
            }
        elif isinstance(raw_out, dict) and raw_out.get("selector"):
            data_out = dict(raw_out)
        else:
            raise LearnError(f"output {oname!r}: give a read step index or {{selector, property}}")
        data_out["selector"] = durable(data_out["selector"], f"output {oname!r}")
        for param in specs:
            if data_out["selector"].get("name"):
                data_out["selector"]["name"], _ = _replace(
                    data_out["selector"]["name"], literals[param], param
                )
        try:
            output_specs[oname] = OutputSpec.model_validate(data_out)
        except ValidationError as exc:
            raise LearnError(f"output {oname!r}: {exc}") from exc

    preconditions: list[Expectation] = []
    first_sel = steps[0].selector
    if first_sel is not None and "{{" not in (first_sel.name or ""):
        preconditions.append(
            Expectation(type="ui_element_exists", selector=first_sel, timeout_ms=3000)
        )

    computed_risk = _risk_of(steps)
    wf_data = {
        "id": skill_id,
        "app_key": app_key,
        "version": version,
        "status": "candidate",
        "name": name,
        "description": description,
        "risk": (
            risk if risk is not None and risk.rank >= computed_risk.rank else computed_risk
        ).value,
        "inputs": {k: v.model_dump(exclude_none=True) for k, v in specs.items()},
        "preconditions": [e.model_dump(exclude_none=True) for e in preconditions],
        "steps": [s.model_dump(exclude_none=True) for s in steps],
        "verification": [e.model_dump(exclude_none=True) for e in verification],
        "outputs": {k: o.model_dump(exclude_none=True) for k, o in output_specs.items()},
        "provenance": Provenance(
            source_execution_id=execution["execution_id"],
            parent_version=parent_version,
            notes=notes,
        ).model_dump(),
    }
    wf = parse_workflow(wf_data)
    unused = set(specs) - wf.template_refs()
    if unused:
        raise LearnError(f"inputs never used by the workflow: {sorted(unused)} (add bindings)")
    return wf, {p: literals[p] for p in specs}
