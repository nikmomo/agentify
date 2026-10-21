"""Deterministic replay of a Workflow through any PlatformAdapter (no LLM involved)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from agentify.adapters.base import (
    Action,
    ActionResult,
    AdapterError,
    AppHandle,
    ElementNotFound,
    PlatformAdapter,
)
from agentify.errors import AgentifyError
from agentify.secrets import has_secret
from agentify.workflows.models import Expectation, SelectorSpec, Step, Workflow
from agentify.workflows.template import render, validate_params
from agentify.workflows.verify import VerificationResult, check, read_output


class StepOutcome(BaseModel):
    step_id: str
    action: str
    ok: bool
    resolved_by: str | None = None
    used_fallback: bool = False
    duration_ms: int = 0
    error: dict[str, Any] | None = None


class Failure(BaseModel):
    category: str
    step_id: str | None = None
    message: str


class RunResult(BaseModel):
    verified: bool
    steps: list[StepOutcome] = Field(default_factory=list)
    verification: list[VerificationResult] = Field(default_factory=list)
    failure: Failure | None = None
    action_count: int = 0
    duration_ms: int = 0
    fallback_used: bool = False
    params: dict[str, str] = Field(default_factory=dict)
    outputs: dict[str, str | None] = Field(default_factory=dict)


def render_selector(sel: SelectorSpec | None, params: dict[str, str]) -> SelectorSpec | None:
    if sel is None:
        return None
    fb = sel.fallback
    if fb is not None:
        fb = fb.model_copy(update={"name": render(fb.name, params)})
    return sel.model_copy(update={"name": render(sel.name, params), "fallback": fb})


def render_step(step: Step, params: dict[str, str]) -> Step:
    return step.model_copy(
        update={
            "selector": render_selector(step.selector, params),
            "value": render(step.value, params),
            "keys": render(step.keys, params),
            "checks": [render_expectation(c, params) for c in step.checks],
        }
    )


def render_expectation(exp: Expectation, params: dict[str, str]) -> Expectation:
    return exp.model_copy(
        update={
            "selector": render_selector(exp.selector, params),
            "expected": render(exp.expected, params),
            "path": render(exp.path, params),
        }
    )


def _action(step: Step, selector: Any) -> Action:
    return Action(
        type=step.action,
        selector=selector,
        pattern=step.pattern,
        value=step.value,
        keys=step.keys,
        via=step.via,
        verbatim=step.verbatim,
        button=step.button,
        double=step.double,
        property_name=step.property_name,
        contains=step.contains,
        gone=step.gone,
        timeout_ms=step.timeout_ms,
        idempotent=step.idempotent,
        # Replay trusts persisted stable automation ids (adapters fall back if not found).
        direct=bool(selector is not None and getattr(selector, "automation_id", None)),
    )


def perform_step(adapter: PlatformAdapter, app: AppHandle, step: Step) -> tuple[ActionResult, bool]:
    """Perform one rendered step: primary selector first, then the fallback if it did not resolve.

    Falling back is safe for non-idempotent steps: ElementNotFound is raised during resolution,
    before any input is sent.
    """
    sel = step.selector
    if sel is None:
        return adapter.perform(app, _action(step, None), timeout_ms=step.timeout_ms), False
    try:
        return adapter.perform(app, _action(step, sel.primary()), timeout_ms=step.timeout_ms), False
    except ElementNotFound:
        if sel.fallback is None:
            raise
        return adapter.perform(app, _action(step, sel.fallback), timeout_ms=step.timeout_ms), True


def run_workflow(
    adapter: PlatformAdapter,
    app: AppHandle,
    workflow: Workflow,
    params: dict[str, Any],
    *,
    max_actions: int = 40,
    max_seconds: float = 180,
    on_step: Callable[[Step, StepOutcome, ActionResult | None], None] | None = None,
    resolve_secret: Callable[[str | None], str | None] | None = None,
) -> RunResult:
    started = time.perf_counter()
    try:
        values = validate_params(workflow.inputs, params)
    except AgentifyError as exc:
        return RunResult(
            verified=False, failure=Failure(category="bad_parameters", message=exc.message)
        )
    # Optional inputs that were omitted render as empty strings.
    values = {name: values.get(name, "") for name in workflow.inputs}
    result = RunResult(verified=False, params=values)
    if len(workflow.steps) > max_actions:
        result.failure = Failure(
            category="limit_exceeded", message=f"workflow has more than {max_actions} steps"
        )
        return result

    for pre in workflow.preconditions:
        check_result = check(adapter, app, render_expectation(pre, values))
        if not check_result.passed:
            result.failure = Failure(
                category="precondition_failed",
                message=f"{pre.type} not satisfied before any input: {check_result.detail or check_result.observed}",
            )
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            return result

    for raw_step in workflow.steps:
        if time.perf_counter() - started > max_seconds:
            result.failure = Failure(
                category="limit_exceeded", step_id=raw_step.id, message="execution time cap reached"
            )
            break
        step = render_step(raw_step, values)
        t0 = time.perf_counter()
        try:
            sent = step
            if resolve_secret is not None and (has_secret(step.value) or has_secret(step.keys)):
                sent = step.model_copy(
                    update={"value": resolve_secret(step.value), "keys": resolve_secret(step.keys)}
                )
            action_result, used_fallback = perform_step(adapter, app, sent)
            outcome = StepOutcome(
                step_id=step.id,
                action=step.action,
                ok=True,
                resolved_by=action_result.resolved_by,
                used_fallback=used_fallback,
                duration_ms=int((time.perf_counter() - t0) * 1000),
            )
            result.fallback_used |= used_fallback
        except AdapterError as exc:
            action_result = None
            outcome = StepOutcome(
                step_id=step.id,
                action=step.action,
                ok=False,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                error={"code": exc.code, "message": exc.message, "details": exc.details},
            )
        result.steps.append(outcome)
        result.action_count += 1
        if on_step:
            on_step(step, outcome, action_result)
        if not outcome.ok and not step.optional:
            assert outcome.error is not None
            result.failure = Failure(
                category=_category_from_code(outcome.error["code"]),
                step_id=step.id,
                message=outcome.error["message"],
            )
            break
        if outcome.ok and step.checks:
            checked = [check(adapter, app, c) for c in step.checks]
            result.verification += checked
            failed_check = next((v for v in checked if not v.passed), None)
            if failed_check is not None:
                result.failure = Failure(
                    category="verification_failed",
                    step_id=step.id,
                    message=f"{failed_check.type} after {step.id}: expected {failed_check.expected!r}, observed {failed_check.observed!r} {failed_check.detail}".strip(),
                )
                break

    if result.failure is None:
        for exp in workflow.verification:
            result.verification.append(check(adapter, app, render_expectation(exp, values)))
        # result.verification also holds the step checks that already passed.
        result.verified = bool(result.verification) and all(v.passed for v in result.verification)
        if not result.verified:
            failed = next(v for v in result.verification if not v.passed)
            result.failure = Failure(
                category="verification_failed",
                message=f"{failed.type}: expected {failed.expected!r}, observed {failed.observed!r} {failed.detail}".strip(),
            )
        if result.verified:
            for name, out in workflow.outputs.items():
                sel = render_selector(out.selector, values)
                assert sel is not None
                result.outputs[name] = settled_output(
                    adapter, app, sel, out.property, set(values.values())
                )
    result.duration_ms = int((time.perf_counter() - started) * 1000)
    return result


OUTPUT_WAIT_S = 10.0
OUTPUT_STABLE_S = 3.0
OUTPUT_POLL_S = 0.3


def settled_output(
    adapter: PlatformAdapter, app: AppHandle, sel: SelectorSpec, prop: str, inputs: set[str]
) -> str | None:
    """Read an output once the app has produced it: a value still equal to one of the inputs
    (e.g. the text just submitted) is not the result yet. Wait until the value differs from the
    inputs and two consecutive reads agree, up to OUTPUT_WAIT_S; then return the last value."""
    norm = {" ".join(str(v).split()) for v in inputs if v}
    start = time.perf_counter()
    deadline = start + OUTPUT_WAIT_S
    last: str | None = None
    same_since = start
    while True:
        try:
            value = read_output(adapter, app, sel, prop)
        except AdapterError:
            value = None
        now = time.perf_counter()
        if value != last:
            same_since = now
        produced = value is not None and value.strip() != "" and " ".join(value.split()) not in norm
        if (produced and value == last) or now - same_since >= OUTPUT_STABLE_S or now >= deadline:
            # A produced value read twice; or a value that has not changed for a while (an
            # output that legitimately equals an input); or the wait is over.
            return value
        last = value
        time.sleep(OUTPUT_POLL_S)


_CODE_CATEGORY = {
    "element_not_found": "selector_unresolved",
    "stale_selector": "selector_unresolved",
    "timeout": "timeout",
    "backend_timeout": "backend_timeout",
    "permission_denied": "permission_denied",
    "window_gone": "app_not_running",
}


def _category_from_code(code: str) -> str:
    return _CODE_CATEGORY.get(code, "action_failed")
