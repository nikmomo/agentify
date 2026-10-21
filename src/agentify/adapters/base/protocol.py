"""The platform adapter protocol. The only layer allowed to touch a GUI."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from agentify.adapters.base.models import (
    Action,
    ActionResult,
    AppHandle,
    AppInfo,
    AppState,
    Element,
    Observation,
    Selector,
)


@runtime_checkable
class PlatformAdapter(Protocol):
    """Contract every adapter implements (see docs/ADAPTER_CONTRACT.md).

    Rules: bounded calls, typed errors only, no retries of non-idempotent actions, no knowledge of
    skills/workflows/applications by name, and no launching or terminating of applications.
    """

    platform: str

    def list_applications(self, *, include_hidden: bool = False) -> list[AppInfo]: ...

    def attach(self, app_id: str) -> AppHandle: ...

    def observe(
        self,
        app: AppHandle,
        *,
        root: Selector | None = None,
        depth: int = 30,
        interactive_only: bool = False,
    ) -> Observation: ...

    def find_elements(
        self, app: AppHandle, selector: Selector, *, max_results: int = 20
    ) -> list[Element]: ...

    def perform(
        self, app: AppHandle, action: Action, *, timeout_ms: int | None = None
    ) -> ActionResult: ...

    def read_value(self, app: AppHandle, selector: Selector) -> str | None: ...

    def get_property(self, app: AppHandle, selector: Selector, name: str) -> Any: ...

    def capture_screenshot(self, app: AppHandle, selector: Selector | None = None) -> Path: ...

    def read_state(self, app: AppHandle) -> AppState: ...
