"""Integration fixtures: real WinApp CLI against our own WinForms fixture app.

The test harness (not the adapter) launches and terminates the fixture process it created.
It never touches processes it did not start.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from agentify.adapters.base import AppHandle
from agentify.adapters.windows import WinAppCliAdapter, WinAppCliRunner

FIXTURE_APP = (
    Path(__file__).resolve().parents[1] / "fixtures" / "ui_change_fixture" / "fixture_app.ps1"
)
FIXTURE_EXE = FIXTURE_APP.parent / "bin" / "AgentifyFixture.exe"
BUILD_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_fixture.ps1"


def ensure_fixture_exe() -> Path:
    if not FIXTURE_EXE.exists():
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(BUILD_SCRIPT)],
            check=True,
            capture_output=True,
        )
    return FIXTURE_EXE


def _require_windows() -> None:
    if sys.platform != "win32":
        pytest.skip("requires Windows")
    if shutil.which("winapp") is None:
        pytest.skip("winapp CLI not on PATH")


@dataclass
class RunningFixture:
    proc: subprocess.Popen[bytes]
    app: AppHandle
    adapter: WinAppCliAdapter
    state_file: Path

    def profile(self) -> dict[str, str]:
        return json.loads(self.state_file.read_text(encoding="utf-8-sig"))


@pytest.fixture(params=["cli", "uia"])
def adapter(request: pytest.FixtureRequest, tmp_path: Path) -> WinAppCliAdapter:
    """Every integration test runs on both backends: WinApp CLI only, and with the in-process
    UI Automation fast path that the server uses by default."""
    _require_windows()
    from agentify.adapters.windows.uia import UiaFastPath

    return WinAppCliAdapter(
        WinAppCliRunner(default_timeout_ms=10_000),
        screenshots_dir=tmp_path / "shots",
        fast_path=UiaFastPath() if request.param == "uia" else None,
    )


def _launch(adapter: WinAppCliAdapter, version: str, exe: bool = False) -> Iterator[RunningFixture]:
    state_dir = Path(tempfile.mkdtemp(prefix="agentify_fixture_"))
    cmd = (
        [str(ensure_fixture_exe()), "-Version", version, "-StateDir", str(state_dir)]
        if exe
        else None
    )
    proc = subprocess.Popen(
        cmd
        or [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(FIXTURE_APP),
            "-Version",
            version,
            "-StateDir",
            str(state_dir),
        ],
    )
    try:
        title = f"Agentify Fixture v{version}"
        deadline = time.monotonic() + 20
        app_info = None
        while time.monotonic() < deadline:
            apps = [
                a
                for a in adapter.list_applications()
                if a.pid == proc.pid and a.window_title == title
            ]
            if apps:
                app_info = apps[0]
                break
            time.sleep(0.3)
        if app_info is None:
            pytest.fail(f"fixture window {title!r} did not appear")
        handle = adapter.attach(app_info.app_id)
        yield RunningFixture(proc, handle, adapter, state_dir / "profile.json")
    finally:
        if proc.poll() is None:
            proc.kill()  # our own child process only
            proc.wait(timeout=10)
        shutil.rmtree(state_dir, ignore_errors=True)


@pytest.fixture
def fixture_a(adapter: WinAppCliAdapter) -> Iterator[RunningFixture]:
    yield from _launch(adapter, "A")


@pytest.fixture
def fixture_b(adapter: WinAppCliAdapter) -> Iterator[RunningFixture]:
    yield from _launch(adapter, "B")


@pytest.fixture
def fixture_exe_a(adapter: WinAppCliAdapter) -> Iterator[RunningFixture]:
    """The same fixture compiled as AgentifyFixture.exe (unique process name -> unique app_key)."""
    yield from _launch(adapter, "A", exe=True)


@pytest.fixture
def workflow_id() -> str:
    return f"agentify-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture(autouse=True)
def _telemetry_optout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WINAPP_CLI_TELEMETRY_OPTOUT", "1")
    os.environ.setdefault("WINAPP_CLI_TELEMETRY_OPTOUT", "1")
