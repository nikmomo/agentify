"""Subprocess wrapper around `winapp ui ...` with timeouts, JSON parsing, and typed error mapping.

Observed WinApp CLI 0.7.1 conventions (2026-10-15):
- Success: exit 0, JSON object on stdout.
- Failure: exit 1, `{"error": {"code", "message", "details"?, "selector"?}}` on stderr.
- `search` with zero matches and `wait-for` timeouts also exit 1 but print a normal JSON payload
  on stdout (`matchCount: 0` / `timedOut: true`); callers opt in with `allow_nonzero=True`.
- Exit 130: interrupted while waiting for a UI turn.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentify.adapters.base.errors import (
    ActionNotSupported,
    AdapterError,
    AdapterTimeout,
    AdapterUnavailable,
    AmbiguousSelector,
    ElementNotFound,
    Interrupted,
    InvalidAction,
    NoInteractiveDesktop,
    PermissionDenied,
    StaleSelector,
    WindowGone,
)
from agentify.adapters.base.keys import redact_keys
from agentify.observability.logging import get_logger

log = get_logger("adapters.windows.runner")

_INPUT_COMMANDS = frozenset(
    {
        "invoke",
        "click",
        "set-value",
        "send-keys",
        "drag",
        "touch",
        "pen",
        "scroll",
        "focus",
        "hover",
    }
)

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class CliCall:
    args: list[str]
    exit_code: int
    duration_ms: int
    stdout: str
    stderr: str
    payload: Any = None
    error: dict[str, Any] | None = None
    redacted_args: list[str] = field(default_factory=list)


class CliRunner(Protocol):
    def run(
        self, args: Sequence[str], *, timeout_ms: int | None = None, allow_nonzero: bool = False
    ) -> CliCall: ...


def _parse_json(text: str) -> Any:
    text = text.strip().lstrip("﻿")
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def redact_args(args: Sequence[str]) -> list[str]:
    """Hide typed text / set values in logs and traces."""
    out = list(args)
    if not out:
        return out
    if out[0] == "set-value" and len(out) >= 3:
        out[2] = f"<{len(out[2])} chars>"
    elif out[0] == "send-keys" and len(out) >= 2:
        out[1] = redact_keys(out[1])
    return out


def map_error(call: CliCall) -> AdapterError:
    """Translate a failed CLI call into a typed adapter error."""
    err = call.error or {}
    code = str(err.get("code") or "")
    message = str(
        err.get("message") or call.stderr.strip() or call.stdout.strip() or "winapp failed"
    )
    details_raw = err.get("details")
    detail = str(details_raw or "")
    info: dict[str, Any] = {
        "cli_code": code,
        "cli_details": details_raw,
        "args": call.redacted_args,
    }

    if call.exit_code == 130:
        return Interrupted(message, details=info)
    if "RuntimeId hash" in message or "UI may have changed" in message:
        return StaleSelector(message, details=info)
    if code == "element_not_found":
        return ElementNotFound(message, details=info)
    if code == "no_interactive_desktop":
        return NoInteractiveDesktop(message, details=info)
    if code == "foreground_not_target":
        return PermissionDenied(message, details=info)
    if code == "invalid_arguments":
        return InvalidAction(message, details=info)
    if "UiAmbiguousSelectorException" in detail or ("matched" in message and "elements" in message):
        return AmbiguousSelector(message, details=info)
    if "AppNotFoundException" in detail or "not found or not accessible" in message:
        return WindowGone(message, details=info)
    if "does not support" in message or "InvalidOperationException" in detail:
        return ActionNotSupported(message, details=info)
    if "UnauthorizedAccess" in detail or "access is denied" in message.lower():
        return PermissionDenied(message, details=info)
    error = AdapterError(message, details=info)
    error.code = code or "cli_error"
    return error


class WinAppCliRunner:
    """Runs `winapp ui <args>` synchronously with a hard timeout."""

    def __init__(
        self,
        executable: str | None = None,
        *,
        default_timeout_ms: int = 15_000,
        startup_grace_ms: int = 10_000,
        workflow_id: str | None = None,
        on_call: Callable[[CliCall], None] | None = None,
    ) -> None:
        self._executable = executable
        self.default_timeout_ms = default_timeout_ms
        self.startup_grace_ms = startup_grace_ms
        self.workflow_id = workflow_id
        self.on_call = on_call

    @property
    def executable(self) -> str:
        if self._executable:
            if not os.path.exists(self._executable) and not shutil.which(self._executable):
                raise AdapterUnavailable(f"winapp executable not found: {self._executable}")
            return self._executable
        found = shutil.which("winapp")
        if not found:
            raise AdapterUnavailable(
                "winapp CLI not found on PATH; install with `winget install Microsoft.WinAppCli`"
            )
        self._executable = found
        return found

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["WINAPP_CLI_TELEMETRY_OPTOUT"] = "1"
        if self.workflow_id:
            env["WINAPP_UI_WORKFLOW_ID"] = self.workflow_id
        return env

    def version(self) -> str:
        proc = subprocess.run(
            [self.executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=self._env(),
            creationflags=_CREATE_NO_WINDOW,
            check=False,
        )
        lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        return lines[-1] if lines else ""

    def run(
        self, args: Sequence[str], *, timeout_ms: int | None = None, allow_nonzero: bool = False
    ) -> CliCall:
        argv = [self.executable, "ui", *args]
        budget_ms = (timeout_ms or self.default_timeout_ms) + self.startup_grace_ms
        redacted = redact_args(args)
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=budget_ms / 1000,
                env=self._env(),
                creationflags=_CREATE_NO_WINDOW,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            command = redacted[0] if redacted else ""
            raise AdapterTimeout(
                f"winapp ui {command} exceeded {budget_ms} ms",
                details={
                    "args": redacted,
                    # An input action that timed out may still have taken effect (e.g. UIA Invoke
                    # on a button that opens a modal dialog blocks until the dialog closes).
                    "possibly_performed": command in _INPUT_COMMANDS,
                },
            ) from exc
        except FileNotFoundError as exc:
            raise AdapterUnavailable(f"cannot execute {self.executable}") from exc
        duration_ms = int((time.perf_counter() - started) * 1000)

        stdout_json = _parse_json(proc.stdout)
        stderr_json = _parse_json(proc.stderr)
        error: dict[str, Any] | None = None
        for blob in (stderr_json, stdout_json):
            if isinstance(blob, dict) and isinstance(blob.get("error"), dict):
                error = blob["error"]
                break

        call = CliCall(
            args=list(args),
            exit_code=proc.returncode,
            duration_ms=duration_ms,
            stdout=proc.stdout,
            stderr=proc.stderr,
            payload=stdout_json,
            error=error,
            redacted_args=redacted,
        )
        log.debug(
            "winapp call",
            extra={"args": redacted, "exit_code": proc.returncode, "duration_ms": duration_ms},
        )
        if self.on_call is not None:
            self.on_call(call)

        if proc.returncode != 0 and (error is not None or not allow_nonzero):
            raise map_error(call)
        return call
