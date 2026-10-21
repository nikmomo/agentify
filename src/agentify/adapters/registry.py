"""Adapter construction from settings, and an honest platform support table."""

from __future__ import annotations

import sys

from agentify.adapters.base.protocol import PlatformAdapter
from agentify.config import Settings, load_settings


def platform_support() -> dict[str, str]:
    return {
        "windows": "supported via Microsoft WinApp CLI (verified on Windows 11 25H2 with WinApp CLI 0.7.1)"
        if sys.platform == "win32"
        else "requires Windows",
        "fake": "test-only in-memory adapter (not evidence of real application support)",
        "browser": "unsupported in MVP",
        "macos": "unsupported in MVP",
        "linux": "unsupported in MVP",
    }


def create_adapter(
    platform: str | None = None, settings: Settings | None = None
) -> PlatformAdapter:
    settings = settings or load_settings()
    name = platform or ("windows" if sys.platform == "win32" else "unsupported")
    if name == "windows":
        from agentify.adapters.windows import WinAppCliAdapter, WinAppCliRunner

        runner = WinAppCliRunner(
            settings.winapp_path,
            default_timeout_ms=settings.winapp_timeout_ms,
            startup_grace_ms=settings.winapp_startup_grace_ms,
        )
        fast = None
        if settings.uia_fast_path:
            from agentify.adapters.windows.uia import UiaFastPath

            fast = UiaFastPath()
        return WinAppCliAdapter(
            runner,
            screenshots_dir=settings.screenshots_dir,
            default_timeout_ms=settings.winapp_timeout_ms,
            default_send_keys_via=settings.default_send_keys_via,
            fast_path=fast,
        )
    if name == "fake":
        from agentify.adapters.fake import FakeAdapter

        return FakeAdapter(screenshots_dir=settings.screenshots_dir)
    from agentify.adapters.unsupported import (
        BrowserAdapter,
        LinuxAdapter,
        MacOSAdapter,
        UnsupportedAdapter,
    )

    cls: type[UnsupportedAdapter] = {
        "browser": BrowserAdapter,
        "macos": MacOSAdapter,
        "linux": LinuxAdapter,
    }.get(name, UnsupportedAdapter)
    return cls()
