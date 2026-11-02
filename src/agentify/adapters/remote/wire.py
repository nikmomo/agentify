"""Wire format and file channel shared by RemoteAdapter (client) and the adapter host.

See docs/ADAPTER_CONTRACT.md "RemoteAdapter". Everything here is platform-independent.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from agentify.adapters.base import errors as E

PROTOCOL = 1
REQUESTS = "requests.jsonl"
RESPONSES = "responses.jsonl"
HEARTBEAT = "host.json"
SHOTS = "shots"
HEARTBEAT_EVERY_S = 2.0
HEARTBEAT_STALE_S = 10.0


def _error_classes() -> dict[str, type[E.AdapterError]]:
    out: dict[str, type[E.AdapterError]] = {}
    stack: list[type[E.AdapterError]] = [E.AdapterError]
    while stack:
        cls = stack.pop()
        out.setdefault(cls.code, cls)
        stack.extend(cls.__subclasses__())
    return out


ERRORS: dict[str, type[E.AdapterError]] = _error_classes()


def error_to_wire(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, E.AdapterError):
        return exc.to_dict()
    return {"code": E.AdapterError.code, "message": f"{type(exc).__name__}: {exc}", "details": {}}


def error_from_wire(err: dict[str, Any]) -> E.AdapterError:
    cls = ERRORS.get(str(err.get("code")), E.AdapterError)
    details = err.get("details")
    return cls(
        str(err.get("message") or "remote adapter error"),
        details=dict(details) if isinstance(details, dict) else {},
    )


def ensure_dir(path: Path) -> None:
    """mkdir -p that trusts the OS's "already exists".

    On a Windows Sandbox mapped folder `Path.mkdir(exist_ok=True)` re-raises FileExistsError:
    it re-checks with is_dir(), which the redirector's metadata cache can still answer False.
    """
    with contextlib.suppress(FileExistsError):
        path.mkdir(parents=True, exist_ok=True)


def append_line(path: Path, obj: dict[str, Any]) -> None:
    """Append one JSON line in one write that the OS places at the end of the file atomically.

    Several clients (threads, and the learning server plus its Explorer's server) append to the
    same requests file. Python's "ab" on Windows seeks to the end and then writes, so two
    appenders can overwrite each other's line (measured: lost requests under three threads).
    A handle opened with FILE_APPEND_DATA only and POSIX O_APPEND both append atomically.
    """
    data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if sys.platform == "win32":
        _append_win32(path, data)
        return
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _append_win32(path: Path, data: bytes) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    write = kernel32.WriteFile
    write.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    write.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    file_append_data, share_all, open_always, normal = 0x0004, 0x7, 4, 0x80
    handle = create(str(path), file_append_data, share_all, None, open_always, normal, None)
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        written = wintypes.DWORD(0)
        if not write(handle, data, len(data), ctypes.byref(written), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if written.value != len(data):
            raise OSError(f"short append to {path}: {written.value} of {len(data)} bytes")
    finally:
        kernel32.CloseHandle(handle)


class LineTail:
    """Lines appended to a file, read through ONE long-lived handle.

    Re-opening a file on a Windows Sandbox mapped folder (or an SMB share) per poll hits the
    redirector's not-found / metadata cache and adds seconds of latency.
    """

    def __init__(self, path: Path, *, from_end: bool) -> None:
        path.touch(exist_ok=True)
        self._f: IO[bytes] = open(path, "rb")  # noqa: SIM115 - held for the tail's lifetime
        if from_end:
            self._f.seek(0, os.SEEK_END)
        self._buf = b""

    def read(self) -> list[str]:
        chunk = self._f.read()
        if not chunk:
            return []
        self._buf += chunk
        *lines, self._buf = self._buf.split(b"\n")
        out = []
        for raw in lines:
            line = raw.decode("utf-8", errors="replace").strip().lstrip("﻿")
            if line:
                out.append(line)
        return out

    def close(self) -> None:
        self._f.close()


def read_heartbeat(rpc_dir: Path) -> dict[str, Any] | None:
    try:
        data = json.loads((rpc_dir / HEARTBEAT).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def heartbeat_age_s(beat: dict[str, Any] | None) -> float | None:
    if not beat or not isinstance(beat.get("beat"), str):
        return None
    try:
        then = datetime.fromisoformat(beat["beat"])
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return (datetime.now(UTC) - then).total_seconds()


def to_shared(path: Path | str | None, rpc_dir: Path) -> str | None:
    """A path as it travels: relative to the shared folder when inside it, else as is."""
    if path is None:
        return None
    p = Path(path)
    try:
        return p.resolve().relative_to(rpc_dir.resolve()).as_posix()
    except ValueError:
        return str(p)


def from_shared(value: str | None, rpc_dir: Path) -> Path | None:
    """A travelled path resolved against this side's view of the shared folder."""
    if value is None:
        return None
    p = Path(value)
    return p if p.is_absolute() else rpc_dir / p
