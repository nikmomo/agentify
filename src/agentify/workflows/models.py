"""Workflow IR models (docs/WORKFLOW_IR.md). Declarative, strict, no code or expressions."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentify.adapters.base.keys import check_keys
from agentify.adapters.base.models import ActionKind, InvokePattern, RiskLevel, Selector
from agentify.workflows.template import template_refs

SkillStatus = Literal["candidate", "active", "superseded", "rejected"]
InputType = Literal["string", "integer", "boolean", "enum", "secret"]

# Actions a workflow may contain. Observation-only actions are evidence, not steps.
STEP_ACTIONS: frozenset[str] = frozenset(
    {"invoke", "set_value", "send_keys", "click", "focus", "wait_for", "scroll_into_view"}
)
SKILL_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,48}$")
INPUT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InputSpec(_Strict):
    type: InputType = "string"
    required: bool = True
    description: str = ""
    example: str | int | bool | None = None
    enum: list[str] | None = None
    pattern: str | None = None
    value_map: dict[str, str] | None = None
    """Accepted value -> literal shown in the UI (e.g. {"ja": "Japanese"}); keys are the allowed values."""

    @model_validator(mode="after")
    def _check(self) -> InputSpec:
        if self.type == "enum" and not (self.enum or self.value_map):
            raise ValueError("enum inputs need a non-empty 'enum' list or a 'value_map'")
        if self.value_map is not None and not self.value_map:
            raise ValueError("value_map must not be empty")
        if self.pattern is not None:
            re.compile(self.pattern)
        return self


class SelectorSpec(Selector):
    """A persisted selector: the primary identity plus an optional fallback tried when it fails."""

    fallback: Selector | None = None

    def primary(self) -> Selector:
        return Selector.model_validate(self.model_dump(exclude={"fallback"}))


class Step(_Strict):
    id: str
    action: ActionKind
    selector: SelectorSpec | None = None
    pattern: InvokePattern | None = None
    value: str | None = None
    keys: str | None = None
    via: Literal["post-message", "send-input"] | None = None
    verbatim: bool = False
    button: Literal["left", "right"] = "left"
    double: bool = False
    property_name: str | None = None
    contains: bool = False
    gone: bool = False
    timeout_ms: int = Field(5000, gt=0, le=120_000)
    idempotent: bool = True
    optional: bool = False
    checks: list[Expectation] = Field(default_factory=list)
    """Postconditions verified right after this step (e.g. a value set on a page that the
    workflow then navigates away from). A failed check fails the run as verification_failed."""


class Expectation(_Strict):
    """An observable postcondition, checked against live state (never the agent's claim)."""

    type: Literal[
        "ui_value_equals",
        "ui_element_exists",
        "ui_element_absent",
        "file_exists",
        "file_text_equals",
    ]
    selector: SelectorSpec | None = None
    property: Literal["value", "name"] = "value"
    expected: str | None = None
    contains: bool = False
    normalize: Literal["none", "trim", "casefold"] = "trim"
    path: str | None = None
    timeout_ms: int = Field(3000, ge=0, le=60_000)

    @model_validator(mode="after")
    def _check(self) -> Expectation:
        if self.type.startswith("ui_") and self.selector is None:
            raise ValueError(f"{self.type} needs a selector")
        if self.type in ("ui_value_equals", "file_text_equals") and self.expected is None:
            raise ValueError(f"{self.type} needs 'expected'")
        if self.type.startswith("file_") and not self.path:
            raise ValueError(f"{self.type} needs 'path'")
        return self


Step.model_rebuild()


class OutputSpec(_Strict):
    """A value the workflow returns, read from the UI after verification passed."""

    selector: SelectorSpec
    property: Literal["value", "name"] = "name"
    description: str = ""


class Provenance(_Strict):
    source_execution_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_by: str = "host-agent"
    parent_version: int | None = None
    notes: str = ""


class Workflow(_Strict):
    schema_version: Literal[1] = 1
    id: str
    app_key: str
    version: int = Field(ge=1)
    status: SkillStatus = "candidate"
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    risk: RiskLevel = RiskLevel.LOCAL_CONFIGURATION_CHANGE
    inputs: dict[str, InputSpec] = Field(default_factory=dict)
    preconditions: list[Expectation] = Field(default_factory=list)
    steps: list[Step]
    verification: list[Expectation]
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    provenance: Provenance = Field(default_factory=Provenance)

    @model_validator(mode="after")
    def _validate(self) -> Workflow:
        if not SKILL_ID_RE.match(self.id):
            raise ValueError(f"skill id {self.id!r} must match {SKILL_ID_RE.pattern}")
        for name in self.inputs:
            if not INPUT_NAME_RE.match(name):
                raise ValueError(f"input name {name!r} must match {INPUT_NAME_RE.pattern}")
        if not self.steps:
            raise ValueError("a workflow needs at least one step")
        if not self.verification and not any(s.checks for s in self.steps):
            raise ValueError("a workflow needs at least one verification (final or step check)")
        ids = [s.id for s in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError("step ids must be unique")
        for step in self.steps:
            if step.action not in STEP_ACTIONS:
                raise ValueError(f"step {step.id}: action {step.action!r} is not a workflow step")
            if step.action == "send_keys" and step.keys:
                check_keys(step.keys, verbatim=step.verbatim)  # deny list (raises PermissionDenied)
        unknown = self.template_refs() - set(self.inputs)
        if unknown:
            raise ValueError(f"templates reference undeclared inputs: {sorted(unknown)}")
        return self

    def template_refs(self) -> set[str]:
        refs: set[str] = set()
        for step in self.steps:
            for text in (step.value, step.keys, *_selector_names(step.selector)):
                refs |= template_refs(text)
        step_checks = [c for s in self.steps for c in s.checks]
        for exp in (*self.verification, *self.preconditions, *step_checks):
            for text in (exp.expected, exp.path, *_selector_names(exp.selector)):
                refs |= template_refs(text)
        for out in self.outputs.values():
            for text in _selector_names(out.selector):
                refs |= template_refs(text)
        return refs


def _selector_names(sel: SelectorSpec | None) -> tuple[str | None, ...]:
    if sel is None:
        return ()
    return (sel.name, sel.fallback.name if sel.fallback else None)
