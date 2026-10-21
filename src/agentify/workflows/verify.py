"""Postcondition checks against live state. "The click returned ok" is never verification."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from agentify.adapters.base import (
    AdapterError,
    AppHandle,
    ElementNotFound,
    PlatformAdapter,
    Selector,
)
from agentify.workflows.models import Expectation, SelectorSpec


class VerificationResult(BaseModel):
    type: str
    passed: bool
    expected: str | None = None
    observed: str | None = None
    detail: str = ""
    resolved_by: str | None = None


def _norm(text: str | None, mode: str) -> str | None:
    if text is None:
        return None
    if mode == "trim":
        return text.strip()
    if mode == "casefold":
        return text.strip().casefold()
    return text


def _candidates(spec: SelectorSpec) -> list[tuple[str, Selector]]:
    out: list[tuple[str, Selector]] = [("primary", spec.primary())]
    if spec.fallback is not None:
        out.append(("fallback", spec.fallback))
    return out


def _read(
    adapter: PlatformAdapter, app: AppHandle, spec: SelectorSpec, prop: str
) -> tuple[str | None, str]:
    last: AdapterError | None = None
    for label, sel in _candidates(spec):
        try:
            if prop == "name":
                value = adapter.get_property(app, sel, "Name")
            else:
                value = adapter.read_value(app, sel)
            return (None if value is None else str(value)), label
        except ElementNotFound as exc:
            last = exc
    assert last is not None
    raise last


def read_output(
    adapter: PlatformAdapter, app: AppHandle, spec: SelectorSpec, prop: str
) -> str | None:
    """Read a workflow output (value or name) from the live UI."""
    return _read(adapter, app, spec, prop)[0]


def _exists(adapter: PlatformAdapter, app: AppHandle, spec: SelectorSpec) -> bool:
    for _label, sel in _candidates(spec):
        try:
            if adapter.find_elements(app, sel, max_results=2):
                return True
        except ElementNotFound:
            continue
    return False


def check(adapter: PlatformAdapter, app: AppHandle, exp: Expectation) -> VerificationResult:
    """Evaluate one (already rendered) expectation, polling up to exp.timeout_ms for async UIs."""
    deadline = time.monotonic() + exp.timeout_ms / 1000
    while True:
        result = _check_once(adapter, app, exp)
        if result.passed or time.monotonic() >= deadline:
            return result
        time.sleep(0.2)


def _check_once(adapter: PlatformAdapter, app: AppHandle, exp: Expectation) -> VerificationResult:
    base: dict[str, Any] = {"type": exp.type, "expected": exp.expected}
    try:
        if exp.type == "ui_value_equals":
            assert exp.selector is not None
            observed, used = _read(adapter, app, exp.selector, exp.property)
            want, got = _norm(exp.expected, exp.normalize), _norm(observed, exp.normalize)
            ok = (
                got is not None
                and want is not None
                and (want in got if exp.contains else got == want)
            )
            return VerificationResult(**base, passed=ok, observed=observed, resolved_by=used)
        if exp.type in ("ui_element_exists", "ui_element_absent"):
            assert exp.selector is not None
            present = _exists(adapter, app, exp.selector)
            ok = present if exp.type == "ui_element_exists" else not present
            return VerificationResult(
                **base, passed=ok, observed="present" if present else "absent"
            )
        path = Path(exp.path or "")
        if exp.type == "file_exists":
            return VerificationResult(**base, passed=path.is_file(), observed=str(path.is_file()))
        if exp.type == "file_text_equals":
            if not path.is_file():
                return VerificationResult(**base, passed=False, detail="file missing")
            text = path.read_text(encoding="utf-8-sig")
            want, got = _norm(exp.expected, exp.normalize), _norm(text, exp.normalize)
            ok = (
                want is not None
                and got is not None
                and (want in got if exp.contains else got == want)
            )
            return VerificationResult(**base, passed=ok, observed=text[:500])
    except AdapterError as exc:
        return VerificationResult(**base, passed=False, detail=f"{exc.code}: {exc.message}")
    raise AssertionError(f"unhandled expectation type {exp.type}")
