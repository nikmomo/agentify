"""`agentify adapter-host`: executes RemoteAdapter requests with a local adapter.

Runs where the app runs (a VM, Windows Sandbox). See docs/ADAPTER_CONTRACT.md "RemoteAdapter".
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentify.adapters.base.errors import AdapterError, ElementNotFound, PermissionDenied
from agentify.adapters.base.models import Action, AppHandle, Selector
from agentify.adapters.base.protocol import PlatformAdapter
from agentify.adapters.remote.wire import (
    HEARTBEAT,
    HEARTBEAT_EVERY_S,
    PROTOCOL,
    REQUESTS,
    RESPONSES,
    SHOTS,
    LineTail,
    append_line,
    ensure_dir,
    error_to_wire,
    to_shared,
)

log = logging.getLogger(__name__)


class AdapterHost:
    """Serves one shared folder with one adapter, one request at a time, in arrival order."""

    def __init__(
        self,
        rpc_dir: Path,
        adapter: PlatformAdapter,
        *,
        allow_launch: Sequence[Path] = (),
        poll_s: float = 0.03,
    ) -> None:
        self.dir = Path(rpc_dir)
        self.adapter = adapter
        self.allow_launch = [Path(p).resolve() for p in allow_launch]
        self.poll_s = poll_s
        self.started = _now()
        self.served = 0
        self._stop = threading.Event()
        ensure_dir(self.dir / SHOTS)
        (self.dir / RESPONSES).touch(exist_ok=True)
        # Requests sent before this host existed were answered by no one; their clients have
        # timed out (they check the heartbeat first), so start at the end.
        self._tail = LineTail(self.dir / REQUESTS, from_end=True)
        self._methods: dict[str, Callable[[dict[str, Any]], Any]] = {
            "ping": self._ping,
            "list_applications": self._list_applications,
            "attach": lambda p: _dump(self.adapter.attach(str(p["app_id"]))),
            "observe": self._observe,
            "find_elements": self._find_elements,
            "perform": self._perform,
            "read_value": lambda p: self.adapter.read_value(_app(p), _selector(p["selector"])),
            "get_property": lambda p: self.adapter.get_property(
                _app(p), _selector(p["selector"]), str(p["name"])
            ),
            "capture_screenshot": self._capture_screenshot,
            "read_state": lambda p: _dump(self.adapter.read_state(_app(p))),
            "ensure_visible": self._ensure_visible,
            "launch_app": self._launch_app,
        }

    # ------------------------------------------------------------------ loop

    def stop(self) -> None:
        self._stop.set()

    def beat(self) -> None:
        data = {
            "pid": os.getpid(),
            "platform": getattr(self.adapter, "platform", sys.platform),
            "protocol": PROTOCOL,
            "started": self.started,
            "beat": _now(),
            "served": self.served,
        }
        tmp = self.dir / (HEARTBEAT + ".tmp")
        try:
            tmp.write_text(json.dumps(data), encoding="utf-8")
            os.replace(tmp, self.dir / HEARTBEAT)
        except OSError:
            log.debug("heartbeat not written", exc_info=True)

    def _beat_loop(self) -> None:
        while not self._stop.wait(HEARTBEAT_EVERY_S):
            self.beat()

    def serve_forever(self) -> None:
        self.beat()
        beater = threading.Thread(target=self._beat_loop, name="adapter-host-beat", daemon=True)
        beater.start()
        log.info("adapter host serving %s", self.dir)
        try:
            while not self._stop.is_set():
                lines = self._tail.read()
                for line in lines:
                    self.handle_line(line)
                if not lines:
                    time.sleep(self.poll_s)
        finally:
            self._stop.set()
            self._tail.close()
            with contextlib.suppress(OSError):
                (self.dir / HEARTBEAT).unlink(missing_ok=True)

    def handle_line(self, line: str) -> None:
        try:
            req = json.loads(line)
        except ValueError:
            log.warning("ignoring a request line that is not JSON")
            return
        if not isinstance(req, dict) or not isinstance(req.get("id"), str):
            return
        rid = req["id"]
        method = str(req.get("method"))
        raw = req.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}
        started = time.perf_counter()
        try:
            if req.get("protocol", PROTOCOL) != PROTOCOL:
                raise AdapterError(
                    f"protocol {req.get('protocol')} is not served (host speaks {PROTOCOL})"
                )
            fn = self._methods.get(method)
            if fn is None:
                raise AdapterError(f"unknown method {method!r}")
            answer: dict[str, Any] = {"id": rid, "ok": True, "result": fn(params)}
        except Exception as exc:  # every failure goes back to the caller, typed when possible
            if not isinstance(exc, AdapterError):
                log.exception("remote %s failed", method)
            answer = {"id": rid, "ok": False, "error": error_to_wire(exc)}
        self.served += 1
        log.debug("remote %s in %.0f ms", method, (time.perf_counter() - started) * 1000)
        append_line(self.dir / RESPONSES, answer)

    # ------------------------------------------------------------------ methods

    def _ping(self, _: dict[str, Any]) -> dict[str, Any]:
        return {
            "platform": getattr(self.adapter, "platform", sys.platform),
            "protocol": PROTOCOL,
            "pid": os.getpid(),
            "launch_dirs": [str(p) for p in self.allow_launch],
        }

    def _list_applications(self, p: dict[str, Any]) -> Any:
        apps = self.adapter.list_applications(include_hidden=bool(p.get("include_hidden")))
        return [_dump(a) for a in apps]

    def _observe(self, p: dict[str, Any]) -> Any:
        root = p.get("root")
        obs = self.adapter.observe(
            _app(p),
            root=None if root is None else _selector(root),
            depth=int(p.get("depth", 30)),
            interactive_only=bool(p.get("interactive_only")),
        )
        return _dump(obs)

    def _find_elements(self, p: dict[str, Any]) -> Any:
        found = self.adapter.find_elements(
            _app(p), _selector(p["selector"]), max_results=int(p.get("max_results", 20))
        )
        return [_dump(e) for e in found]

    def _perform(self, p: dict[str, Any]) -> Any:
        timeout = p.get("timeout_ms")
        result = self.adapter.perform(
            _app(p),
            Action.model_validate(p["action"]),
            timeout_ms=None if timeout is None else int(timeout),
        )
        data = _dump(result)
        data["screenshot_path"] = to_shared(result.screenshot_path, self.dir)
        return data

    def _capture_screenshot(self, p: dict[str, Any]) -> Any:
        sel = p.get("selector")
        path = self.adapter.capture_screenshot(_app(p), None if sel is None else _selector(sel))
        return to_shared(path, self.dir)

    def _ensure_visible(self, p: dict[str, Any]) -> Any:
        fn = getattr(self.adapter, "ensure_visible", None)
        if fn is None:
            return True
        return bool(fn(_app(p), timeout_s=float(p.get("timeout_s", 4.0))))

    def _launch_app(self, p: dict[str, Any]) -> Any:
        """A Start-menu shortcut by name, or an executable under an --allow-launch folder."""
        app = str(p.get("app") or "").strip()
        if not app:
            raise ElementNotFound("launch_app needs an app name or executable path")
        exe = Path(app)
        if exe.suffix.lower() == ".exe" and exe.is_absolute():
            target = exe.resolve()
            if not any(target.is_relative_to(d) for d in self.allow_launch):
                raise PermissionDenied(
                    f"{target} is outside the folders this host may launch from",
                    details={"allow_launch": [str(d) for d in self.allow_launch]},
                )
            if not target.is_file():
                raise ElementNotFound(f"{target} does not exist on the host")
            subprocess.Popen([str(target)], cwd=str(target.parent), close_fds=True)
            return {"launched": target.stem, "via": "exe"}
        if sys.platform != "win32":
            raise ElementNotFound(f"no launcher for {app!r} on {sys.platform}")
        from agentify.adapters.windows.launch import find_shortcut, launch_shortcut

        link = find_shortcut(app)
        if link is None:
            raise ElementNotFound(f"{app!r} has no Start-menu shortcut on the host")
        launch_shortcut(link)
        return {"launched": link.stem, "via": "shortcut"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _dump(model: Any) -> Any:
    return model.model_dump(mode="json")


def _app(p: dict[str, Any]) -> AppHandle:
    return AppHandle.model_validate(p["app"])


def _selector(data: Any) -> Selector:
    return Selector.model_validate(data)
