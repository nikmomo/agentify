"""Typed observation/action models shared by every platform adapter.

These models are platform-independent. Platform-specific details (HWNDs, WinApp CLI slugs) appear
only as opaque, session-scoped values and must never be persisted as durable identifiers.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RiskLevel(StrEnum):
    READ_ONLY = "read_only"
    LOCAL_CONFIGURATION_CHANGE = "local_configuration_change"
    DATA_WRITE = "data_write"
    DESTRUCTIVE = "destructive"
    EXTERNAL_EFFECT = "external_effect"

    @property
    def rank(self) -> int:
        return _RISK_ORDER.index(self)

    def at_least(self, other: RiskLevel) -> bool:
        return self.rank >= other.rank


_RISK_ORDER: list[RiskLevel] = [
    RiskLevel.READ_ONLY,
    RiskLevel.LOCAL_CONFIGURATION_CHANGE,
    RiskLevel.DATA_WRITE,
    RiskLevel.DESTRUCTIVE,
    RiskLevel.EXTERNAL_EFFECT,
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Bounds(_Model):
    x: int
    y: int
    width: int
    height: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + self.height // 2)


class Selector(_Model):
    """Semantic element selector.

    Resolution precedence: automation_id -> slug -> (role, name). `slug` is session-scoped
    (WinApp CLI hash suffixes change across launches), so durable selectors must carry
    role+name or a stable automation_id.
    """

    automation_id: str | None = None
    slug: str | None = None
    role: str | None = None
    name: str | None = None
    exact: bool = False
    root: Selector | None = None
    index: int | None = Field(default=None, ge=-1000, le=1000)
    """0-based position among matches; negative counts from the end (-1 = the last one)."""
    label: str | None = None
    """For unnamed controls: the visible text right before the control (e.g. a form label). Matches
    the first element of `role` that follows, in tree order, an element named `label`."""

    @model_validator(mode="after")
    def _require_identity(self) -> Selector:
        positional = bool(self.role) and (self.index is not None or bool(self.label))
        if not (self.automation_id or self.slug or self.name or positional):
            raise ValueError(
                "selector needs automation_id, slug, or name (or role + index for unnamed controls)"
            )
        return self

    def describe(self) -> str:
        parts = []
        if self.automation_id:
            parts.append(f"automation_id={self.automation_id!r}")
        if self.slug:
            parts.append(f"slug={self.slug!r}")
        if self.role:
            parts.append(f"role={self.role!r}")
        if self.name:
            parts.append(f"name{'==' if self.exact else '~'}{self.name!r}")
        if self.label:
            parts.append(f"label={self.label!r}")
        if self.index is not None:
            parts.append(f"index={self.index}")
        if self.root:
            parts.append(f"root=({self.root.describe()})")
        return ", ".join(parts)


class Element(_Model):
    role: str
    name: str | None = None
    automation_id: str | None = None
    class_name: str | None = None
    slug: str | None = None
    """Session-scoped backend selector (WinApp CLI: automation id when unique, else semantic slug)."""
    value: str | None = None
    bounds: Bounds | None = None
    enabled: bool = True
    offscreen: bool = False
    invokable: bool = False
    expand_state: str | None = None
    ancestor_path: tuple[str, ...] = ()
    children: tuple[Element, ...] = ()
    has_more_children: bool = False

    @property
    def has_stable_automation_id(self) -> bool:
        """WinForms exposes HWND-derived numeric automation ids for unnamed controls; those change per launch."""
        return bool(self.automation_id) and not str(self.automation_id).isdigit()

    def iter_tree(self) -> Iterator[Element]:
        yield self
        for child in self.children:
            yield from child.iter_tree()

    def durable_selector(self) -> Selector:
        """Best selector to persist for replay: stable automation id plus role+name fallback data."""
        if self.has_stable_automation_id:
            return Selector(automation_id=self.automation_id, role=self.role, name=self.name)
        if self.name:
            return Selector(role=self.role, name=self.name, exact=True)
        if self.slug:
            return Selector(slug=self.slug, role=self.role)
        raise ValueError(f"element of role {self.role!r} has no durable identity")


class WindowInfo(_Model):
    hwnd: int
    title: str
    class_name: str | None = None
    dpi: int | None = None
    scale: float | None = None
    dpi_awareness: str | None = None
    coordinate_space: str | None = None


class ObservedWindow(_Model):
    info: WindowInfo
    elements: tuple[Element, ...] = ()
    element_count: int | None = None


class Observation(_Model):
    app_id: str
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    windows: tuple[ObservedWindow, ...] = ()
    screenshot_path: Path | None = None

    def iter_elements(self) -> Iterator[Element]:
        for window in self.windows:
            for element in window.elements:
                yield from element.iter_tree()


class AppInfo(_Model):
    app_id: str
    platform: str
    process_name: str
    pid: int
    hwnd: int
    window_title: str
    window_class: str | None = None
    width: int | None = None
    height: int | None = None
    owner_hwnd: int | None = None
    is_foreground: bool = False


class AppHandle(_Model):
    app_id: str
    platform: str
    hwnd: int
    pid: int
    process_name: str
    window_title: str
    dpi_scale: float | None = None
    dpi_awareness: str | None = None


class AppState(_Model):
    app_id: str
    alive: bool
    window_title: str | None = None
    is_foreground: bool | None = None
    width: int | None = None
    height: int | None = None
    dpi_scale: float | None = None


ActionKind = Literal[
    "locate",
    "invoke",
    "set_value",
    "send_keys",
    "click",
    "focus",
    "wait_for",
    "scroll_into_view",
    "screenshot",
    "read_value",
]

InvokePattern = Literal[
    "invoke", "select", "toggle", "toggle-on", "toggle-off", "expand", "collapse"
]

_SELECTOR_REQUIRED: frozenset[str] = frozenset(
    {
        "locate",
        "invoke",
        "set_value",
        "click",
        "focus",
        "wait_for",
        "scroll_into_view",
        "read_value",
    }
)

_DEFAULT_RISK: dict[str, RiskLevel] = {
    "locate": RiskLevel.READ_ONLY,
    "read_value": RiskLevel.READ_ONLY,
    "screenshot": RiskLevel.READ_ONLY,
    "wait_for": RiskLevel.READ_ONLY,
    "focus": RiskLevel.READ_ONLY,
    "scroll_into_view": RiskLevel.READ_ONLY,
    "invoke": RiskLevel.LOCAL_CONFIGURATION_CHANGE,
    "set_value": RiskLevel.LOCAL_CONFIGURATION_CHANGE,
    "send_keys": RiskLevel.LOCAL_CONFIGURATION_CHANGE,
    "click": RiskLevel.LOCAL_CONFIGURATION_CHANGE,
}


class Action(_Model):
    type: ActionKind
    selector: Selector | None = None
    value: str | None = None
    keys: str | None = None
    pattern: InvokePattern | None = None
    via: Literal["post-message", "send-input"] | None = None
    verbatim: bool = False
    button: Literal["left", "right"] = "left"
    double: bool = False
    gone: bool = False
    contains: bool = False
    property_name: str | None = None
    timeout_ms: int | None = Field(default=None, gt=0, le=120_000)
    idempotent: bool = True
    risk: RiskLevel | None = None
    direct: bool = False
    """Hint for replay: the selector's automation id is trusted, so an adapter may act on it without a
    separate lookup, falling back to full resolution if the element is not found. Adapters may ignore
    it."""

    @model_validator(mode="after")
    def _check_fields(self) -> Action:
        if self.type in _SELECTOR_REQUIRED and self.selector is None:
            raise ValueError(f"action {self.type!r} requires a selector")
        if self.type == "set_value" and self.value is None:
            raise ValueError("set_value requires value")
        if self.type == "send_keys" and not self.keys:
            raise ValueError("send_keys requires keys")
        if self.pattern is not None and self.type != "invoke":
            raise ValueError("pattern is only valid for invoke")
        return self

    @property
    def effective_risk(self) -> RiskLevel:
        return self.risk or _DEFAULT_RISK[self.type]


class ActionResult(_Model):
    ok: bool
    action_type: ActionKind
    resolved_by: str | None = None
    element: Element | None = None
    value: str | None = None
    screenshot_path: Path | None = None
    duration_ms: int
    target_hwnd: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)
