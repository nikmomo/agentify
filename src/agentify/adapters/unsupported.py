"""Adapters for platforms that are designed for but not implemented in the MVP.

Every method raises UnsupportedPlatformError, so callers can report availability honestly instead
of receiving mocked results.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, NoReturn

from agentify.adapters.base.errors import UnsupportedPlatformError
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


class UnsupportedAdapter:
    platform = "unsupported"
    planned_backend = ""

    def _fail(self) -> NoReturn:
        raise UnsupportedPlatformError(
            f"{self.platform} adapter is not implemented in the MVP (planned: {self.planned_backend})"
        )

    def list_applications(self, *, include_hidden: bool = False) -> list[AppInfo]:
        self._fail()

    def attach(self, app_id: str) -> AppHandle:
        self._fail()

    def observe(
        self,
        app: AppHandle,
        *,
        root: Selector | None = None,
        depth: int = 30,
        interactive_only: bool = False,
    ) -> Observation:
        self._fail()

    def find_elements(
        self, app: AppHandle, selector: Selector, *, max_results: int = 20
    ) -> list[Element]:
        self._fail()

    def perform(
        self, app: AppHandle, action: Action, *, timeout_ms: int | None = None
    ) -> ActionResult:
        self._fail()

    def read_value(self, app: AppHandle, selector: Selector) -> str | None:
        self._fail()

    def get_property(self, app: AppHandle, selector: Selector, name: str) -> Any:
        self._fail()

    def capture_screenshot(self, app: AppHandle, selector: Selector | None = None) -> Path:
        self._fail()

    def read_state(self, app: AppHandle) -> AppState:
        self._fail()


class BrowserAdapter(UnsupportedAdapter):
    platform = "browser"
    planned_backend = "Playwright accessibility tree"


class MacOSAdapter(UnsupportedAdapter):
    platform = "macos"
    planned_backend = "macOS Accessibility API (AXUIElement)"


class LinuxAdapter(UnsupportedAdapter):
    platform = "linux"
    planned_backend = "AT-SPI accessibility"
