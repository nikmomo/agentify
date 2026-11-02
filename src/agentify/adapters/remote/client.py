"""RemoteAdapter: the platform adapter protocol, executed by an adapter host elsewhere."""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agentify.adapters.base.errors import AdapterTimeout, AdapterUnavailable
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
from agentify.adapters.remote.wire import (
    HEARTBEAT_STALE_S,
    PROTOCOL,
    REQUESTS,
    RESPONSES,
    SHOTS,
    LineTail,
    append_line,
    ensure_dir,
    error_from_wire,
    from_shared,
    heartbeat_age_s,
    read_heartbeat,
)

# Added to an action's own timeout: the host's backend has its own startup grace.
_ACTION_GRACE_MS = 30_000


class RemoteAdapter:
    """Forwards every adapter call to `agentify adapter-host` over a shared folder.

    Thread-safe: concurrent calls share one response reader and each takes only its own answer.
    """

    platform = "windows"
    remote = True

    def __init__(
        self,
        rpc_dir: Path,
        *,
        timeout_ms: int = 60_000,
        poll_s: float = 0.03,
        action_grace_ms: int = _ACTION_GRACE_MS,
    ) -> None:
        self.dir = Path(rpc_dir)
        self.timeout_ms = timeout_ms
        self.action_grace_ms = action_grace_ms
        self.poll_s = poll_s
        ensure_dir(self.dir / SHOTS)
        (self.dir / REQUESTS).touch(exist_ok=True)
        self._tail = LineTail(self.dir / RESPONSES, from_end=True)
        self._lock = threading.Lock()
        self._mine: set[str] = set()
        self._answers: dict[str, dict[str, Any]] = {}
        # (monotonic time of the beat, the beat) from the last successful heartbeat read.
        self._last_beat: tuple[float, dict[str, Any]] | None = None

    # ------------------------------------------------------------------ transport

    def host_status(self) -> dict[str, Any] | None:
        """The host's heartbeat, or None when no host has served this folder recently.

        The host replaces host.json atomically, but on Windows a read that lands during the
        replace fails with a sharing violation. A few quick retries, and a fresh beat seen
        within the stale window, keep that from reading as "no host".
        """
        for attempt in range(5):
            beat = read_heartbeat(self.dir)
            age = heartbeat_age_s(beat)
            if beat is not None and age is not None:
                if age > HEARTBEAT_STALE_S:
                    return None
                self._last_beat = (time.monotonic() - age, beat)
                return beat
            if attempt < 4:
                time.sleep(0.02)
        if self._last_beat and time.monotonic() - self._last_beat[0] <= HEARTBEAT_STALE_S:
            return self._last_beat[1]
        return None

    def _call(self, method: str, params: dict[str, Any], timeout_ms: int | None = None) -> Any:
        if self.host_status() is None:
            raise AdapterUnavailable(
                f"no adapter host is serving {self.dir}: run `agentify adapter-host --dir <that "
                "folder as the app's machine sees it>` where the app runs",
                details={"rpc_dir": str(self.dir)},
            )
        rid = uuid.uuid4().hex
        budget = timeout_ms or self.timeout_ms
        with self._lock:
            self._mine.add(rid)
        append_line(
            self.dir / REQUESTS,
            {"id": rid, "method": method, "params": params, "protocol": PROTOCOL},
        )
        deadline = time.monotonic() + budget / 1000
        try:
            while True:
                with self._lock:
                    for line in self._tail.read():
                        try:
                            msg = json.loads(line)
                        except ValueError:
                            continue
                        mid = msg.get("id") if isinstance(msg, dict) else None
                        if mid in self._mine:
                            self._answers[mid] = msg
                    answer = self._answers.pop(rid, None)
                if answer is not None:
                    break
                if time.monotonic() > deadline:
                    raise AdapterTimeout(
                        f"remote {method} got no answer in {budget} ms",
                        details={
                            "possibly_performed": method == "perform",
                            "host_alive": self.host_status() is not None,
                        },
                    )
                time.sleep(self.poll_s)
        finally:
            with self._lock:
                self._mine.discard(rid)
        if not answer.get("ok"):
            err = answer.get("error")
            raise error_from_wire(err if isinstance(err, dict) else {})
        return answer.get("result")

    def close(self) -> None:
        self._tail.close()

    # ------------------------------------------------------------------ protocol

    def ping(self) -> dict[str, Any]:
        result = self._call("ping", {})
        return result if isinstance(result, dict) else {}

    def list_applications(self, *, include_hidden: bool = False) -> list[AppInfo]:
        result = self._call("list_applications", {"include_hidden": include_hidden})
        return [AppInfo.model_validate(a) for a in result or []]

    def attach(self, app_id: str) -> AppHandle:
        return AppHandle.model_validate(self._call("attach", {"app_id": app_id}))

    def observe(
        self,
        app: AppHandle,
        *,
        root: Selector | None = None,
        depth: int = 30,
        interactive_only: bool = False,
    ) -> Observation:
        params = {
            "app": _dump(app),
            "root": _dump(root),
            "depth": depth,
            "interactive_only": interactive_only,
        }
        return Observation.model_validate(self._call("observe", params))

    def find_elements(
        self, app: AppHandle, selector: Selector, *, max_results: int = 20
    ) -> list[Element]:
        params = {"app": _dump(app), "selector": _dump(selector), "max_results": max_results}
        return [Element.model_validate(e) for e in self._call("find_elements", params) or []]

    def perform(
        self, app: AppHandle, action: Action, *, timeout_ms: int | None = None
    ) -> ActionResult:
        own = timeout_ms or action.timeout_ms or 0
        budget = max(self.timeout_ms, own + self.action_grace_ms)
        params = {"app": _dump(app), "action": _dump(action), "timeout_ms": timeout_ms}
        data = dict(self._call("perform", params, timeout_ms=budget) or {})
        data["screenshot_path"] = _path_or_none(from_shared(data.get("screenshot_path"), self.dir))
        return ActionResult.model_validate(data)

    def read_value(self, app: AppHandle, selector: Selector) -> str | None:
        result = self._call("read_value", {"app": _dump(app), "selector": _dump(selector)})
        return None if result is None else str(result)

    def get_property(self, app: AppHandle, selector: Selector, name: str) -> Any:
        return self._call(
            "get_property", {"app": _dump(app), "selector": _dump(selector), "name": name}
        )

    def capture_screenshot(self, app: AppHandle, selector: Selector | None = None) -> Path:
        result = self._call("capture_screenshot", {"app": _dump(app), "selector": _dump(selector)})
        path = from_shared(result if isinstance(result, str) else None, self.dir)
        if path is None:
            raise AdapterUnavailable("the adapter host returned no screenshot path")
        return path

    def read_state(self, app: AppHandle) -> AppState:
        return AppState.model_validate(self._call("read_state", {"app": _dump(app)}))

    # ------------------------------------------------------------------ extras

    def ensure_visible(self, app: AppHandle, timeout_s: float = 4.0) -> bool:
        budget = max(self.timeout_ms, int(timeout_s * 1000) + self.action_grace_ms)
        params = {"app": _dump(app), "timeout_s": timeout_s}
        return bool(self._call("ensure_visible", params, timeout_ms=budget))

    def launch_app(self, app: str) -> dict[str, Any]:
        """Ask the host to open `app` (an executable path or a Start-menu shortcut name)."""
        result = self._call("launch_app", {"app": app})
        return result if isinstance(result, dict) else {}


def _dump(model: Any) -> Any:
    return None if model is None else model.model_dump(mode="json")


def _path_or_none(p: Path | None) -> str | None:
    return None if p is None else str(p)
