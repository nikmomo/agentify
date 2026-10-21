"""WinAppCliAdapter: the Windows implementation of PlatformAdapter over Microsoft WinApp CLI 0.7.1.

Design notes (all verified on 2026-10-15 unless marked):
- Every call is scoped with `-w <hwnd>` of the attached window, never `-a <name>`.
- `invoke`/`set-value`/`click` accept no `--type` filter, so selectors are first resolved with
  `search` (which does accept `--type`/`--root`) and the action then uses the session selector the
  CLI returned for the chosen element.
- Dropdown items live in popup windows with their own HWND (ComboLBox); the outside-window guard
  therefore compares owning process ids, not HWNDs.
- `send-keys` defaults to `--via send-input`: post-message typing was silently ignored by a
  WinForms TextBox.
- UIA Invoke on a button that opens a *modal* dialog blocks until the dialog closes (WinForms
  ShowDialog). Use `click` for such buttons; an invoke timeout reports `possibly_performed=True`.
- A stale slug yields a CLI "RuntimeId hash doesn't match" error, surfaced as StaleSelector and
  followed by the next resolution strategy.
- `observe` defaults to depth 30 without `--interactive`: WebView2 (e.g. Tauri) content sits
  ~20 levels deep and the CLI's interactive filter prunes it entirely (verified 2026-10-16).
- The adapter never launches or terminates applications.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentify.adapters.base.errors import (
    AdapterError,
    AdapterTimeout,
    ElementNotFound,
    InvalidAction,
    PermissionDenied,
    WindowGone,
)
from agentify.adapters.base.keys import check_keys
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
from agentify.adapters.base.selectors import filter_matches, label_matches, pick, strategies
from agentify.adapters.windows import parse
from agentify.adapters.windows.runner import CliRunner, WinAppCliRunner
from agentify.adapters.windows.uia import FAST_ACTIONS, UiaFastPath, UiaTimeout, UiaUnavailable
from agentify.adapters.windows.uia import send_keys as uia_send_keys
from agentify.adapters.windows.win32 import activate_window, window_process_id
from agentify.observability.logging import get_logger

log = get_logger("adapters.windows")

# Shell/system surfaces hidden from list_applications unless include_system=True.
# This is a platform-level filter (Windows shell components), not application-specific logic.
_SYSTEM_PROCESSES = frozenset(
    {
        "textinputhost",
        "searchhost",
        "startmenuexperiencehost",
        "shellexperiencehost",
        "applicationframehost",
        "lockapp",
    }
)


class ResolvedElement:
    __slots__ = ("cli_selector", "element", "resolved_by")

    def __init__(self, element: Element, cli_selector: str, resolved_by: str) -> None:
        self.element = element
        self.cli_selector = cli_selector
        self.resolved_by = resolved_by


class WinAppCliAdapter:
    platform = "windows"

    def __init__(
        self,
        runner: CliRunner | None = None,
        *,
        screenshots_dir: Path = Path("./data/screenshots"),
        default_timeout_ms: int = 15_000,
        default_send_keys_via: str = "send-input",
        search_max: int = 50,
        include_system_windows: bool = False,
        enforce_process_scope: bool = True,
        warm_accessibility: bool = True,
        activate_for_input: bool = True,
        activator: Callable[[int], bool] | None = None,
        fast_path: UiaFastPath | None = None,
    ) -> None:
        self.runner: CliRunner = runner or WinAppCliRunner(default_timeout_ms=default_timeout_ms)
        self.screenshots_dir = screenshots_dir
        self.default_timeout_ms = default_timeout_ms
        self.default_send_keys_via = default_send_keys_via
        self.search_max = search_max
        self.include_system_windows = include_system_windows
        self.enforce_process_scope = enforce_process_scope
        self.warm_accessibility = warm_accessibility
        # Real-input actions need the attached window in the foreground (the CLI refuses otherwise).
        self.activate_for_input = activate_for_input
        self._activate = activator or activate_window
        # In-process UI Automation for hot operations; None = CLI only.
        self.fast_path = fast_path
        self._attached: dict[int, AppHandle] = {}  # live windows already attached (fast path only)

    # ------------------------------------------------------------------ discovery

    def list_applications(self, *, include_hidden: bool = False) -> list[AppInfo]:
        args = ["list-windows", "--json"]
        if include_hidden:
            args.append("--show-hidden")
        call = self.runner.run(args)
        items = call.payload if isinstance(call.payload, list) else []
        apps = [parse.parse_app_info(d) for d in items if isinstance(d, dict)]
        if not self.include_system_windows:
            apps = [
                a
                for a in apps
                if a.window_title
                and a.process_name.lower() not in _SYSTEM_PROCESSES
                and not (
                    a.process_name.lower() == "explorer" and a.window_title == "Program Manager"
                )
            ]
        return apps

    def attach(self, app_id: str) -> AppHandle:
        try:
            expected_proc, hwnd = parse.parse_app_id(app_id)
        except ValueError as exc:
            raise InvalidAction(
                f"app_id must be 'process:hwnd' or an hwnd, got {app_id!r}"
            ) from exc
        cached = self._attached.get(hwnd)
        if cached is not None and window_process_id(hwnd) == cached.pid:
            # Same live window (a reused handle would belong to another process): no CLI round
            # trips, and its accessibility tree is already warm.
            return cached
        call = self.runner.run(["status", "-w", str(hwnd), "--json"])
        status = call.payload if isinstance(call.payload, dict) else {}
        proc = str(status.get("processName") or "")
        if expected_proc and proc and proc.lower() != expected_proc.lower():
            raise WindowGone(
                f"hwnd {hwnd} now belongs to {proc!r}, not {expected_proc!r} (window handle reused)"
            )
        if self.warm_accessibility:
            self._warm_accessibility(hwnd)
        handle = AppHandle(
            app_id=parse.make_app_id(proc or (expected_proc or "unknown"), hwnd),
            platform=self.platform,
            hwnd=hwnd,
            pid=int(status.get("processId", 0)),
            process_name=proc or (expected_proc or "unknown"),
            window_title=str(status.get("windowTitle") or ""),
            dpi_scale=float(status["scale"]) if status.get("scale") is not None else None,
            dpi_awareness=status.get("dpiAwareness"),
        )
        if self.fast_path is not None and handle.pid:
            self._attached[hwnd] = handle
        return handle

    def _warm_accessibility(self, hwnd: int) -> None:
        """Make Chromium-based UIs (WebView2, Electron) build their full accessibility tree.

        Verified 2026-10-16 on a WebView2 app: the first UIA query after launch returned a shallow
        tree (28 elements, no web controls); every later query returned the full tree (74). One
        discarded deep inspect at attach time makes the first real observation complete.
        """
        try:
            self.runner.run(
                ["inspect", "-w", str(hwnd), "--depth", "30", "--json"], allow_nonzero=True
            )
        except AdapterError as exc:
            log.debug("accessibility warm-up failed", extra={"hwnd": hwnd, "code": exc.code})

    def read_state(self, app: AppHandle) -> AppState:
        call = self.runner.run(["list-windows", "--json", "--show-hidden"])
        for d in call.payload if isinstance(call.payload, list) else []:
            if isinstance(d, dict) and int(d.get("hwnd", 0)) == app.hwnd:
                return AppState(
                    app_id=app.app_id,
                    alive=True,
                    window_title=d.get("title"),
                    is_foreground=bool(d.get("isForeground", False)),
                    width=d.get("width"),
                    height=d.get("height"),
                    dpi_scale=app.dpi_scale,
                )
        return AppState(app_id=app.app_id, alive=False)

    # ------------------------------------------------------------------ observation

    def observe(
        self,
        app: AppHandle,
        *,
        root: Selector | None = None,
        depth: int = 30,
        interactive_only: bool = False,
    ) -> Observation:
        args = ["inspect"]
        if root is not None:
            args.append(self.resolve(app, root).cli_selector)
        args += [*self._w(app), "--depth", str(depth), "--json"]
        if interactive_only:
            args.append("--interactive")
        call = self.runner.run(args)
        payload = call.payload if isinstance(call.payload, dict) else {}
        obs = parse.parse_observation(app.app_id, payload)
        if root is None and self._looks_unrendered(obs) and self._rerender(app):
            call = self.runner.run(args)
            payload = call.payload if isinstance(call.payload, dict) else {}
            obs = parse.parse_observation(app.app_id, payload)
        return obs

    @staticmethod
    def _looks_unrendered(obs: Observation) -> bool:
        """WebView2/Chromium windows that are occluded stop rendering and expose an almost empty
        tree: only window chrome (title bar, system menu, caption buttons, empty panes)."""
        chrome_ids = {"TitleBar", "SystemMenuBar", "Minimize-Restore", "Maximize-Restore", "Close"}
        content = [
            e
            for e in obs.iter_elements()
            if e.role not in ("Window", "TitleBar", "MenuBar", "MenuItem")
            and e.automation_id not in chrome_ids
            and not (e.role == "Pane" and not e.children)
        ]
        return not content

    def _rerender(self, app: AppHandle) -> bool:
        """Bring the attached window forward so it renders again (only that window)."""
        if not self.activate_for_input:
            return False
        ok = self._activate(app.hwnd)
        time.sleep(1.0)
        return ok

    def find_elements(
        self, app: AppHandle, selector: Selector, *, max_results: int = 20
    ) -> list[Element]:
        if self.fast_path is not None:
            try:
                found, trusted = self.fast_path.find(app.hwnd, selector)
                if trusted:
                    return found[:max_results]
            except (UiaUnavailable, UiaTimeout):
                pass
        for strategy in strategies(selector):
            matches = self._candidates(app, strategy.name, strategy.selector)
            if matches:
                return matches[:max_results]
        return []

    def resolve(self, app: AppHandle, selector: Selector) -> ResolvedElement:
        """Resolve a selector to exactly one element, trying strategies in precedence order.

        If nothing matches and the window's tree is empty (an occluded WebView2 app), the window
        is re-rendered once and resolution retried.
        """
        try:
            return self._resolve(app, selector)
        except ElementNotFound:
            if not self.activate_for_input:
                raise
            probe = self.runner.run(
                ["inspect", *self._w(app), "--depth", "8", "--json"], allow_nonzero=True
            )
            payload = probe.payload if isinstance(probe.payload, dict) else {}
            if not self._looks_unrendered(parse.parse_observation(app.app_id, payload)):
                raise
            if not self._rerender(app):
                raise
            return self._resolve(app, selector)

    def _resolve(self, app: AppHandle, selector: Selector) -> ResolvedElement:
        tried: list[str] = []
        for strategy in strategies(selector):
            tried.append(strategy.name)
            matches = self._candidates(app, strategy.name, strategy.selector)
            if not matches:
                continue
            element = pick(matches, strategy.selector)
            if not element.slug:
                raise ElementNotFound(
                    f"matched element has no CLI selector ({selector.describe()})"
                )
            return ResolvedElement(element, element.slug, strategy.name)
        raise ElementNotFound(
            f"no element matches ({selector.describe()})", details={"strategies_tried": tried}
        )

    def _candidates(self, app: AppHandle, strategy: str, sel: Selector) -> list[Element]:
        if strategy in ("role_index", "role_label"):
            # The CLI's search needs text, so unnamed controls are found in the full tree.
            ordered = list(self.observe(app, root=sel.root).iter_elements())
            if strategy == "role_label":
                return label_matches(ordered, sel)
            return filter_matches(ordered, sel)
        query = sel.automation_id or sel.slug or sel.name or ""
        args = ["search", query, *self._w(app), "--max", str(self.search_max), "--json"]
        if strategy == "role_name" and sel.role:
            args += ["--type", sel.role]
        if sel.root is not None:
            args += ["--root", self.resolve(app, sel.root).cli_selector]
        try:
            call = self.runner.run(args, allow_nonzero=True)
        except ElementNotFound:  # includes StaleSelector: let the next strategy try
            return []
        found = parse.parse_search(call.payload)
        if strategy == "slug":
            # The CLI resolves slugs itself and may report the element under its automation id,
            # so only the role (if given) is re-checked here.
            return [
                e for e in found if sel.role is None or e.role.casefold() == sel.role.casefold()
            ]
        return filter_matches(found, sel)

    # ------------------------------------------------------------------ actions

    def perform(
        self, app: AppHandle, action: Action, *, timeout_ms: int | None = None
    ) -> ActionResult:
        if self.fast_path is not None and action.type in FAST_ACTIONS and action.selector:
            fast = self._perform_fast(app, action, timeout_ms)
            if fast is not None:
                return fast
        if (
            self.fast_path is not None
            and action.type == "send_keys"
            and (action.via or self.default_send_keys_via) == "send-input"
        ):
            fast = self._send_keys_fast(app, action)
            if fast is not None:
                return fast
        sel = action.selector
        if (
            action.direct
            and sel is not None
            and sel.automation_id
            and not sel.automation_id.isdigit()
            and sel.root is None
            and action.type not in ("wait_for", "screenshot", "locate")
        ):
            direct = ResolvedElement(
                Element(role=sel.role or "Unknown", name=sel.name, automation_id=sel.automation_id),
                sel.automation_id,
                "automation_id_direct",
            )
            try:
                return self._perform(app, action, timeout_ms, direct)
            except ElementNotFound:
                # The CLI did not find the element, so no input was sent: resolve fully and retry.
                log.debug("direct automation id %s not found; resolving", sel.automation_id)
        return self._perform(app, action, timeout_ms, None)

    def _send_keys_fast(self, app: AppHandle, action: Action) -> ActionResult | None:
        started = time.perf_counter()
        check_keys(action.keys or "", verbatim=action.verbatim)  # the deny list, as on the CLI path
        element, how = None, None
        try:
            if action.selector is not None:
                assert self.fast_path is not None
                element, _, how = self.fast_path.perform(
                    app.hwnd, app.pid, Action(type="focus", selector=action.selector), 5000
                )
            uia_send_keys(app.hwnd, action.keys or "", verbatim=action.verbatim)
            time.sleep(0.15)  # let the UI react before the next step looks at it
        except (UiaUnavailable, ElementNotFound) as exc:
            log.debug("uia send_keys unavailable: %s", exc)
            return None
        return self._result(action, started, how, element, None, None, {"backend": "uia"})

    def _perform_fast(
        self, app: AppHandle, action: Action, timeout_ms: int | None
    ) -> ActionResult | None:
        """The UIA fast path; None means "use the CLI path" (unsupported, or not found there)."""
        assert self.fast_path is not None
        timeout = timeout_ms or action.timeout_ms or self.default_timeout_ms
        started = time.perf_counter()
        if action.type == "focus":
            self._ensure_foreground(app)
        try:
            element, value, how = self.fast_path.perform(app.hwnd, app.pid, action, timeout)
        except UiaUnavailable as exc:
            log.debug("uia fast path unavailable for %s: %s", action.type, exc)
            return None
        except ElementNotFound as exc:
            by_id = bool(action.selector and action.selector.automation_id)
            if exc.details.get("rendered") and not by_id:
                raise  # a real miss in a rendered window: no slower second look
            # An automation id the fast path cannot see (window chrome such as the caption's
            # Close button comes from a separate non-client provider): the CLI path looks.
            return None  # maybe an occluded WebView: the CLI path re-renders it and looks again
        except UiaTimeout:
            if action.type in ("locate", "read_value"):
                return None  # read-only: the CLI path can look instead
            raise
        except AdapterTimeout:
            if action.type != "wait_for":
                raise
            # Confirm with one quick CLI check before reporting the timeout.
            try:
                return self._perform(app, action, 300, None)
            except AdapterError:
                raise AdapterTimeout(
                    f"wait_for timed out after {timeout} ms ({action.selector.describe() if action.selector else ''})"
                ) from None
        return self._result(action, started, how, element, value, None, {"backend": "uia"})

    def _perform(
        self,
        app: AppHandle,
        action: Action,
        timeout_ms: int | None,
        preresolved: ResolvedElement | None,
    ) -> ActionResult:
        timeout = timeout_ms or action.timeout_ms or self.default_timeout_ms
        started = time.perf_counter()
        resolved: ResolvedElement | None = None
        value: str | None = None
        screenshot: Path | None = None
        payload: dict[str, Any] = {}

        kind = action.type
        if kind == "send_keys":
            check_keys(action.keys or "", verbatim=action.verbatim)

        if kind == "wait_for":
            assert action.selector is not None
            payload, element = self._wait_for(app, action, timeout)
            return self._result(action, started, None, element, None, None, payload)

        if kind == "screenshot":
            screenshot = self.capture_screenshot(app, action.selector)
            return self._result(action, started, None, None, None, screenshot, {})

        if preresolved is not None:
            resolved = preresolved
        elif action.selector is not None:
            resolved = self.resolve(app, action.selector)

        w = self._w(app)
        if kind == "locate":
            pass
        elif kind == "invoke":
            assert resolved is not None
            args = ["invoke", resolved.cli_selector, *w, "--json"]
            if action.pattern:
                args += ["--action", action.pattern]
            payload = self._run_dict(args, timeout)
        elif kind == "set_value":
            assert resolved is not None and action.value is not None
            payload = self._run_dict(
                ["set-value", resolved.cli_selector, action.value, *w, "--json"], timeout
            )
        elif kind == "send_keys":
            if (action.via or self.default_send_keys_via) == "send-input":
                self._ensure_foreground(app)
            args = ["send-keys", action.keys or "", *w, "--json"]
            args += ["--via", action.via or self.default_send_keys_via]
            if resolved is not None:
                args += ["--target", resolved.cli_selector]
            if action.verbatim:
                args.append("--verbatim")
            payload = self._run_dict(args, timeout)
        elif kind == "click":
            assert resolved is not None
            self._ensure_foreground(app)
            args = ["click", resolved.cli_selector, *w, "--json"]
            if action.double:
                args.append("--double")
            if action.button == "right":
                args.append("--right")
            payload = self._run_dict(args, timeout)
        elif kind == "focus":
            self._ensure_foreground(app)
            # WinApp CLI 0.7.1 requires an element selector for focus.
            assert resolved is not None
            args = ["focus", resolved.cli_selector, *w, "--json"]
            payload = self._run_dict(args, timeout)
        elif kind == "scroll_into_view":
            assert resolved is not None
            payload = self._run_dict(
                ["scroll-into-view", resolved.cli_selector, *w, "--json"], timeout
            )
        elif kind == "read_value":
            assert resolved is not None
            payload = self._run_dict(["get-value", resolved.cli_selector, *w, "--json"], timeout)
            value = _opt(payload.get("text"))
        else:  # pragma: no cover - Literal exhausts kinds
            raise InvalidAction(f"unsupported action type {kind!r}")

        self._check_scope(app, payload)
        return self._result(
            action,
            started,
            resolved.resolved_by if resolved else None,
            resolved.element if resolved else None,
            value,
            screenshot,
            payload,
        )

    def _wait_for(
        self, app: AppHandle, action: Action, timeout_ms: int
    ) -> tuple[dict[str, Any], Element | None]:
        sel = action.selector
        assert sel is not None
        last_error: AdapterError | None = None
        deadline = time.perf_counter() + timeout_ms / 1000
        for strategy in strategies(sel):
            remaining = max(100, int((deadline - time.perf_counter()) * 1000))
            s = strategy.selector
            query = s.automation_id or s.slug or s.name or ""
            args = ["wait-for", query, *self._w(app), "-t", str(remaining), "--json"]
            if strategy.name == "role_name" and s.role:
                args += ["--type", s.role]
            if s.root is not None:
                args += ["--root", self.resolve(app, s.root).cli_selector]
            if action.gone:
                args.append("--gone")
            if action.value is not None:
                args += ["--value", action.value]
                if action.property_name:
                    args += ["--property", action.property_name]
                if action.contains:
                    args.append("--contains")
            call = self.runner.run(args, timeout_ms=remaining, allow_nonzero=True)
            payload = call.payload if isinstance(call.payload, dict) else {}
            timed_out = bool(payload.get("timedOut")) or call.exit_code != 0
            if timed_out or (not action.gone and not payload.get("found")):
                last_error = AdapterTimeout(
                    f"wait_for timed out after {payload.get('waitedMs', remaining)} ms ({sel.describe()})",
                    details={"strategy": strategy.name},
                )
                continue
            element_d = payload.get("element")
            element = parse.parse_element(element_d) if isinstance(element_d, dict) else None
            if element is not None and not action.gone and not filter_matches([element], s):
                last_error = ElementNotFound(
                    f"wait_for found a non-matching element ({sel.describe()})",
                    details={"found": element.model_dump(exclude={"children"})},
                )
                continue
            return payload, element
        raise last_error or AdapterTimeout(f"wait_for timed out ({sel.describe()})")

    # ------------------------------------------------------------------ reads

    def read_value(self, app: AppHandle, selector: Selector) -> str | None:
        return self.perform(app, Action(type="read_value", selector=selector)).value

    def get_property(self, app: AppHandle, selector: Selector, name: str) -> Any:
        if self.fast_path is not None:
            try:
                return self.fast_path.read(app.hwnd, selector, name)
            except (UiaUnavailable, ElementNotFound, UiaTimeout):
                pass
        resolved = self.resolve(app, selector)
        payload = self._run_dict(
            ["get-property", resolved.cli_selector, "-p", name, *self._w(app), "--json"],
            self.default_timeout_ms,
        )
        props = payload.get("properties") or {}
        return props.get(name)

    def capture_screenshot(self, app: AppHandle, selector: Selector | None = None) -> Path:
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        out = (self.screenshots_dir / f"{app.process_name}-{app.hwnd}-{stamp}.png").resolve()
        args = ["screenshot"]
        if selector is not None:
            args.append(self.resolve(app, selector).cli_selector)
        args += [*self._w(app), "-o", str(out), "--json"]
        payload = self._run_dict(args, self.default_timeout_ms)
        return Path(str(payload.get("filePath") or out))

    def yield_turn(self) -> None:
        """Release the WinApp CLI UI turn early (only meaningful with a workflow id)."""
        if isinstance(self.runner, WinAppCliRunner) and self.runner.workflow_id:
            self.runner.run(["yield", "--json"], allow_nonzero=True)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _w(app: AppHandle) -> list[str]:
        return ["-w", str(app.hwnd)]

    def _run_dict(self, args: list[str], timeout_ms: int) -> dict[str, Any]:
        call = self.runner.run(args, timeout_ms=timeout_ms)
        return call.payload if isinstance(call.payload, dict) else {}

    def ensure_visible(self, app: AppHandle, timeout_s: float = 4.0) -> bool:
        """Restore the attached window if minimized, bring it to the foreground, and wait until
        its content is rendered (a restored WebView paints a moment later). Returns True when
        the window shows content. Only the attached window is touched."""
        import ctypes

        user32 = ctypes.windll.user32
        was_minimized = bool(user32.IsIconic(ctypes.c_void_p(app.hwnd)))
        if was_minimized:
            user32.ShowWindow(ctypes.c_void_p(app.hwnd), 9)  # SW_RESTORE
        if self.activate_for_input:
            self._activate(app.hwnd)
        if not was_minimized:
            return True
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            time.sleep(0.25)
            try:
                if not self._looks_unrendered(self.observe(app, depth=12)):
                    time.sleep(0.3)  # let the first paint settle
                    return True
            except AdapterError:
                continue
        return False

    def _ensure_foreground(self, app: AppHandle) -> None:
        """Activate the attached window (only that window) before real input."""
        if self.activate_for_input and not self._activate(app.hwnd):
            log.debug("could not activate hwnd %s before input", app.hwnd)

    def _check_scope(self, app: AppHandle, payload: dict[str, Any]) -> None:
        """Detective guard: the acted-on window must belong to the attached process."""
        target = payload.get("hwnd")
        if not self.enforce_process_scope or not target or int(target) == app.hwnd:
            return
        pid = window_process_id(int(target))
        if pid is not None and pid != app.pid:
            log.error(
                "action reached a window outside the attached process",
                extra={"attached_pid": app.pid, "target_hwnd": target, "target_pid": pid},
            )
            raise PermissionDenied(
                f"action targeted hwnd {target} owned by pid {pid}, outside attached pid {app.pid}",
                details={"target_hwnd": target, "target_pid": pid},
            )

    @staticmethod
    def _result(
        action: Action,
        started: float,
        resolved_by: str | None,
        element: Element | None,
        value: str | None,
        screenshot: Path | None,
        payload: dict[str, Any],
    ) -> ActionResult:
        target = payload.get("hwnd")
        return ActionResult(
            ok=True,
            action_type=action.type,
            resolved_by=resolved_by,
            element=element,
            value=value,
            screenshot_path=screenshot,
            duration_ms=int((time.perf_counter() - started) * 1000),
            target_hwnd=int(target) if target else None,
            raw=payload,
        )


def _opt(value: Any) -> str | None:
    return None if value is None else str(value)
