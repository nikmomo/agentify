"""Window allowlist: restrict an adapter to specific top-level windows.

When the user has other windows of the same app open (e.g. a Notepad window with their own unsaved
tabs), choosing "the foreground or largest window" could act on the wrong one. With an allowlist,
only the listed windows are visible (list_applications) or attachable (attach); every other
operation needs a handle from attach, so nothing else can be touched.
"""

from __future__ import annotations

from typing import Any

from agentify.adapters.base.errors import PermissionDenied
from agentify.adapters.base.models import AppHandle, AppInfo
from agentify.adapters.base.protocol import PlatformAdapter


def parse_allowlist(raw: str) -> frozenset[str]:
    """Comma-separated app ids ("notepad:133456"), window handles ("133456"), or process names
    ("notepad": every window of that process), case-insensitive."""
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


class WindowAllowlistAdapter:
    def __init__(self, inner: PlatformAdapter, allowed: frozenset[str]) -> None:
        self._inner = inner
        self._allowed = allowed

    def _ok(self, app_id: str, hwnd: int | None) -> bool:
        process = app_id.split(":", 1)[0].lower()
        return (
            app_id.lower() in self._allowed
            or (hwnd is not None and str(hwnd) in self._allowed)
            or process in self._allowed  # a process name: its windows (handles change per launch)
        )

    def list_applications(self, *, include_hidden: bool = False) -> list[AppInfo]:
        apps = self._inner.list_applications(include_hidden=include_hidden)
        return [a for a in apps if self._ok(a.app_id, a.hwnd)]

    def attach(self, app_id: str) -> AppHandle:
        handle = self._inner.attach(app_id)
        if not self._ok(handle.app_id, handle.hwnd):
            raise PermissionDenied(
                f"window {app_id!r} is not in AGENTIFY_ALLOWED_WINDOWS",
                details={"allowed": sorted(self._allowed)},
            )
        return handle

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_inner", "_allowed"):
            object.__setattr__(self, name, value)
        else:  # e.g. switching the inner adapter's fast path off for one run
            setattr(self._inner, name, value)
