"""Typed adapter errors. Every adapter failure surfaces as one of these, never as a raw exception."""

from __future__ import annotations

from typing import Any


class AdapterError(Exception):
    """Base class. `code` is a stable machine-readable identifier used in MCP error envelopes."""

    code: str = "adapter_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ElementNotFound(AdapterError):
    code = "element_not_found"


class StaleSelector(ElementNotFound):
    """A session selector (slug) no longer matches the element it was minted for.

    WinApp CLI 0.7.1 reports this when an element is found by name but its RuntimeId hash differs,
    which is a direct signal that the UI changed or the app was relaunched.
    """

    code = "stale_selector"


class AmbiguousSelector(AdapterError):
    code = "ambiguous_selector"


class ActionNotSupported(AdapterError):
    """The element exists but does not support the requested operation (e.g. missing UIA pattern)."""

    code = "action_not_supported"


class WindowGone(AdapterError):
    code = "window_gone"


class NoInteractiveDesktop(AdapterError):
    code = "no_interactive_desktop"


class AdapterTimeout(AdapterError):
    code = "timeout"


class PermissionDenied(AdapterError):
    """Blocked by policy (system keys, outside attached window) or by the OS (UIPI)."""

    code = "permission_denied"


class AdapterUnavailable(AdapterError):
    """The backend (e.g. the winapp executable) is missing or unusable."""

    code = "adapter_unavailable"


class InvalidAction(AdapterError):
    """The action is malformed or the backend rejected its arguments."""

    code = "invalid_action"


class Interrupted(AdapterError):
    code = "interrupted"


class UnsupportedPlatformError(AdapterError):
    code = "unsupported_platform"
