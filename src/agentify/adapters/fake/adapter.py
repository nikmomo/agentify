"""FakeAdapter: an in-memory desktop with a tiny control state machine.

Mirrors the selector semantics of WinAppCliAdapter (same strategies, same matching, same typed
errors) so runtime/workflow/lifecycle logic can be unit-tested deterministically. Slugs carry a
per-launch hash, so `FakeDesktop.relaunch()` reproduces the observed real-world behaviour that
WinApp CLI slugs change across application launches.

A passing FakeAdapter test proves logic only. It is never evidence of Windows UI support.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentify.adapters.base.errors import (
    ActionNotSupported,
    AdapterTimeout,
    InvalidAction,
    WindowGone,
)
from agentify.adapters.base.keys import check_keys
from agentify.adapters.base.models import (
    Action,
    ActionResult,
    AppHandle,
    AppInfo,
    AppState,
    Bounds,
    Element,
    Observation,
    ObservedWindow,
    Selector,
    WindowInfo,
)
from agentify.adapters.base.selectors import filter_matches, label_matches, pick, strategies

Handler = Callable[["FakeApp", "FakeNode"], None]

_PREFIX = {
    "Button": "btn",
    "ComboBox": "cmb",
    "Edit": "txt",
    "Document": "doc",
    "Text": "lbl",
    "ListItem": "itm",
    "List": "lst",
    "RadioButton": "rad",
    "CheckBox": "chk",
    "Window": "win",
    "MenuItem": "mnu",
    "TabItem": "tab",
}


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())[:16] or "x"


@dataclass(eq=False)
class FakeNode:
    role: str
    name: str | None = None
    automation_id: str | None = None
    value: str | None = None
    enabled: bool = True
    items: list[str] | None = None
    """ComboBox items; exposed as ListItem children only while expanded (like WinForms)."""
    expanded: bool = False
    selected: bool = False
    group: str | None = None
    """RadioButton group name; selecting one deselects the others in the same window."""
    on_invoke: Handler | None = None
    children: list[FakeNode] = field(default_factory=list)
    _item_nodes: dict[str, FakeNode] = field(default_factory=dict, repr=False)

    @property
    def supports_value(self) -> bool:
        return self.role in {"Edit", "Document", "ComboBox"}

    def visible_children(self) -> list[FakeNode]:
        kids = list(self.children)
        if self.role == "ComboBox" and self.expanded and self.items:
            for item in self.items:
                node = self._item_nodes.get(item)
                if node is None:
                    node = FakeNode(role="ListItem", name=item)
                    self._item_nodes[item] = node
                node.selected = item == self.value
                kids.append(node)
        return kids


@dataclass(eq=False)
class FakeWindow:
    hwnd: int
    title: str
    nodes: list[FakeNode] = field(default_factory=list)
    class_name: str = "FakeWindow"
    modal: bool = False


@dataclass(eq=False)
class FakeApp:
    process_name: str
    pid: int
    windows: list[FakeWindow]
    state: dict[str, Any] = field(default_factory=dict)
    state_file: Path | None = None
    launch_seed: int = 0
    alive: bool = True
    focused: FakeNode | None = None
    key_log: list[str] = field(default_factory=list)

    @property
    def main(self) -> FakeWindow:
        return self.windows[0]

    @property
    def app_id(self) -> str:
        return f"{self.process_name}:{self.main.hwnd}"

    def open_window(self, window: FakeWindow) -> None:
        self.windows.append(window)

    def close_window(self, title: str) -> None:
        self.windows = [w for w in self.windows if w.title != title or w is self.main]

    def find_node(self, *, name: str | None = None, automation_id: str | None = None) -> FakeNode:
        for node, _parent, _window, _path in self.walk():
            if (name is None or node.name == name) and (
                automation_id is None or node.automation_id == automation_id
            ):
                return node
        raise KeyError(name or automation_id)

    def walk(self) -> Iterator[tuple[FakeNode, FakeNode | None, FakeWindow, str]]:
        def rec(
            node: FakeNode, parent: FakeNode | None, window: FakeWindow, path: str
        ) -> Iterator[tuple[FakeNode, FakeNode | None, FakeWindow, str]]:
            yield node, parent, window, path
            for i, child in enumerate(node.visible_children()):
                yield from rec(child, node, window, f"{path}.{i}")

        for wi, window in enumerate(self.windows):
            for ni, node in enumerate(window.nodes):
                yield from rec(node, None, window, f"{wi}.{ni}")

    def write_state(self) -> None:
        if self.state_file is not None:
            import json

            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, sort_keys=True), encoding="utf-8")
            tmp.replace(self.state_file)


class FakeDesktop:
    def __init__(self) -> None:
        self.apps: dict[str, FakeApp] = {}
        self._next_hwnd = 1000

    def add(self, app: FakeApp) -> FakeApp:
        self.apps[app.app_id] = app
        return app

    def next_hwnd(self) -> int:
        self._next_hwnd += 2
        return self._next_hwnd

    def relaunch(self, app_id: str) -> None:
        """Simulate an application restart: same UI, new session slugs."""
        self.apps[app_id].launch_seed += 1

    def close(self, app_id: str) -> None:
        self.apps[app_id].alive = False


class FakeAdapter:
    platform = "fake"

    def __init__(
        self, desktop: FakeDesktop | None = None, *, screenshots_dir: Path | None = None
    ) -> None:
        self.desktop = desktop or FakeDesktop()
        self.screenshots_dir = screenshots_dir
        self.calls: list[Action] = []

    # ---------------------------------------------------------------- discovery

    def list_applications(self, *, include_hidden: bool = False) -> list[AppInfo]:
        return [
            AppInfo(
                app_id=app.app_id,
                platform=self.platform,
                process_name=app.process_name,
                pid=app.pid,
                hwnd=app.main.hwnd,
                window_title=app.main.title,
                window_class=app.main.class_name,
            )
            for app in self.desktop.apps.values()
            if app.alive
        ]

    def attach(self, app_id: str) -> AppHandle:
        app = self._app_by_id(app_id)
        return AppHandle(
            app_id=app.app_id,
            platform=self.platform,
            hwnd=app.main.hwnd,
            pid=app.pid,
            process_name=app.process_name,
            window_title=app.main.title,
            dpi_scale=1.0,
        )

    def read_state(self, app: AppHandle) -> AppState:
        fake = self.desktop.apps.get(app.app_id)
        if fake is None or not fake.alive:
            return AppState(app_id=app.app_id, alive=False)
        return AppState(
            app_id=app.app_id,
            alive=True,
            window_title=fake.main.title,
            is_foreground=True,
            dpi_scale=1.0,
        )

    # ---------------------------------------------------------------- observation

    def observe(
        self,
        app: AppHandle,
        *,
        root: Selector | None = None,
        depth: int = 30,
        interactive_only: bool = False,
    ) -> Observation:
        fake = self._app(app)
        windows = []
        root_node = self._resolve_node(fake, root)[0] if root is not None else None
        for wi, window in enumerate(fake.windows):
            elements: tuple[Element, ...]
            if root_node is not None:
                found = [(n, p) for n, _pa, w, p in fake.walk() if n is root_node and w is window]
                if not found:
                    continue
                elements = (self._to_element(fake, found[0][0], found[0][1], depth),)
            else:
                elements = tuple(
                    self._to_element(fake, n, f"{wi}.{ni}", depth)
                    for ni, n in enumerate(window.nodes)
                )
            windows.append(
                ObservedWindow(
                    info=WindowInfo(
                        hwnd=window.hwnd,
                        title=window.title,
                        class_name=window.class_name,
                        scale=1.0,
                    ),
                    elements=elements,
                )
            )
        return Observation(app_id=app.app_id, windows=tuple(windows))

    def find_elements(
        self, app: AppHandle, selector: Selector, *, max_results: int = 20
    ) -> list[Element]:
        fake = self._app(app)
        for strategy in strategies(selector):
            matches = filter_matches(self._all_elements(fake, selector.root), strategy.selector)
            if matches:
                return matches[:max_results]
        return []

    # ---------------------------------------------------------------- actions

    def perform(
        self, app: AppHandle, action: Action, *, timeout_ms: int | None = None
    ) -> ActionResult:
        started = time.perf_counter()
        fake = self._app(app)
        self.calls.append(action)
        kind = action.type

        if kind == "send_keys":
            check_keys(action.keys or "", verbatim=action.verbatim)
        if kind == "wait_for":
            return self._wait_for(fake, action, started)
        if kind == "screenshot":
            return self._result(
                action, started, None, None, None, self.capture_screenshot(app, action.selector)
            )

        node: FakeNode | None = None
        element: Element | None = None
        resolved_by: str | None = None
        if action.selector is not None:
            node, element, resolved_by = self._resolve_node(fake, action.selector)

        value: str | None = None
        if kind == "locate":
            pass
        elif kind in ("invoke", "click"):
            assert node is not None
            self._invoke(fake, node, action.pattern if kind == "invoke" else None)
        elif kind == "set_value":
            assert node is not None and action.value is not None
            self._set_value(node, action.value)
        elif kind == "send_keys":
            target = node or fake.focused
            self._send_keys(fake, target, action.keys or "", verbatim=action.verbatim)
        elif kind == "focus":
            fake.focused = node
        elif kind == "scroll_into_view":
            pass
        elif kind == "read_value":
            assert node is not None
            value = node.value if node.value is not None else node.name
        return self._result(action, started, resolved_by, element, value, None)

    def read_value(self, app: AppHandle, selector: Selector) -> str | None:
        return self.perform(app, Action(type="read_value", selector=selector)).value

    def get_property(self, app: AppHandle, selector: Selector, name: str) -> Any:
        node, _el, _by = self._resolve_node(self._app(app), selector)
        return {
            "Name": node.name,
            "Value": node.value,
            "AutomationId": node.automation_id,
            "IsEnabled": node.enabled,
            "ExpandCollapseState": "Expanded" if node.expanded else "Collapsed",
            "IsSelected": node.selected,
        }.get(name)

    def capture_screenshot(self, app: AppHandle, selector: Selector | None = None) -> Path:
        from PIL import Image

        directory = self.screenshots_dir or Path("./data/screenshots")
        directory.mkdir(parents=True, exist_ok=True)
        out = directory / f"fake-{app.hwnd}-{len(self.calls)}.png"
        Image.new("RGB", (4, 4), (200, 200, 200)).save(out)
        return out

    # ---------------------------------------------------------------- behaviour

    @staticmethod
    def _invoke(fake: FakeApp, node: FakeNode, pattern: str | None) -> None:
        if not node.enabled:
            raise ActionNotSupported(f"{node.role} {node.name!r} is disabled")
        if pattern in ("expand", "collapse"):
            if node.role != "ComboBox":
                raise ActionNotSupported(f"{node.role} does not support ExpandCollapsePattern")
            node.expanded = pattern == "expand"
            return
        if pattern == "select" or (pattern is None and node.role in ("ListItem", "RadioButton")):
            FakeAdapter._select(fake, node)
            return
        if pattern in ("toggle", "toggle-on", "toggle-off"):
            if node.role != "CheckBox":
                raise ActionNotSupported(f"{node.role} does not support TogglePattern")
            node.selected = {"toggle": not node.selected, "toggle-on": True, "toggle-off": False}[
                pattern
            ]
            return
        if node.on_invoke is not None:
            node.on_invoke(fake, node)
            return
        if node.role == "ComboBox":
            node.expanded = not node.expanded
            return
        raise ActionNotSupported(f"{node.role} {node.name!r} has no invoke behaviour")

    @staticmethod
    def _select(fake: FakeApp, node: FakeNode) -> None:
        for candidate, parent, window, _path in fake.walk():
            if candidate is node:
                if parent is not None and parent.role == "ComboBox":
                    parent.value = node.name
                    parent.expanded = False
                elif node.role == "RadioButton":
                    for other, _p, w, _pp in fake.walk():
                        if (
                            w is window
                            and other.role == "RadioButton"
                            and other.group == node.group
                        ):
                            other.selected = False
                    node.selected = True
                else:
                    raise ActionNotSupported(f"{node.role} does not support SelectionItemPattern")
                return

    @staticmethod
    def _set_value(node: FakeNode, value: str) -> None:
        if not node.supports_value:
            raise ActionNotSupported(f"{node.role} does not support ValuePattern")
        if node.role == "ComboBox" and node.items is not None and value not in node.items:
            raise InvalidAction(f"{value!r} is not an item of ComboBox {node.name!r}")
        node.value = value

    @staticmethod
    def _send_keys(fake: FakeApp, target: FakeNode | None, keys: str, *, verbatim: bool) -> None:
        fake.key_log.append(keys)
        if verbatim:
            texts = [keys]
        else:
            texts = [
                t[5:].replace("\\s", " ").replace("\\t", "\t").replace("\\n", "\n")
                for t in keys.split()
                if t.lower().startswith("text=")
            ]
        if texts:
            if target is None or not target.supports_value or target.role == "ComboBox":
                raise ActionNotSupported("send_keys text needs a focused or targeted text element")
            target.value = (target.value or "") + "".join(texts)

    def _wait_for(self, fake: FakeApp, action: Action, started: float) -> ActionResult:
        sel = action.selector
        assert sel is not None
        elements = self._all_elements(fake, sel.root)
        prop = action.property_name or "Value"
        want = action.value

        def value_ok(e: Element) -> bool:
            if want is None:
                return True
            have = (e.name if prop == "Name" else e.value) or ""
            return want in have if action.contains else have == want

        found: dict[str, list[Element]] = {
            s.name: [e for e in filter_matches(elements, s.selector) if value_ok(e)]
            for s in strategies(sel)
        }
        if action.gone:
            if not any(found.values()):
                return self._result(action, started, None, None, None, None)
        else:
            for name, matches in found.items():
                if matches:
                    return self._result(action, started, name, matches[0], None, None)
        raise AdapterTimeout(f"wait_for condition not met ({sel.describe()})")

    # ---------------------------------------------------------------- resolution

    def _resolve_node(self, fake: FakeApp, selector: Selector) -> tuple[FakeNode, Element, str]:
        nodes = self._index(fake, selector.root)
        for strategy in strategies(selector):
            ordered = [el for el, _n in nodes]
            matches = (
                label_matches(ordered, strategy.selector)
                if strategy.name == "role_label"
                else filter_matches(ordered, strategy.selector)
            )
            if matches:
                chosen = pick(matches, strategy.selector)
                node = next(n for el, n in nodes if el is chosen)
                return node, chosen, strategy.name
        from agentify.adapters.base.errors import ElementNotFound

        raise ElementNotFound(f"no element matches ({selector.describe()})")

    def _index(self, fake: FakeApp, root: Selector | None) -> list[tuple[Element, FakeNode]]:
        entries = [(self._to_element(fake, n, p, 0), n) for n, _pa, _w, p in fake.walk()]
        if root is None:
            return entries
        root_node = self._resolve_node(fake, root)[0]
        inside: set[int] = set()

        def mark(node: FakeNode) -> None:
            for child in node.visible_children():
                inside.add(id(child))
                mark(child)

        mark(root_node)
        return [(e, n) for e, n in entries if id(n) in inside]

    def _all_elements(self, fake: FakeApp, root: Selector | None) -> list[Element]:
        return [e for e, _n in self._index(fake, root)]

    @staticmethod
    def _slug(fake: FakeApp, node: FakeNode, path: str) -> str:
        if node.automation_id and not node.automation_id.isdigit():
            return node.automation_id
        digest = hashlib.sha1(f"{fake.launch_seed}|{path}".encode()).hexdigest()[:4]
        base = _slugify(node.name or node.automation_id or node.role)
        return f"{_PREFIX.get(node.role, 'el')}-{base}-{digest}"

    def _to_element(self, fake: FakeApp, node: FakeNode, path: str, depth: int) -> Element:
        children: tuple[Element, ...] = ()
        if depth > 0:
            children = tuple(
                self._to_element(fake, c, f"{path}.{i}", depth - 1)
                for i, c in enumerate(node.visible_children())
            )
        return Element(
            role=node.role,
            name=node.name,
            automation_id=node.automation_id,
            slug=self._slug(fake, node, path),
            value=node.value,
            bounds=Bounds(x=0, y=0, width=100, height=20),
            enabled=node.enabled,
            invokable=node.on_invoke is not None
            or node.role in ("Button", "ListItem", "RadioButton"),
            expand_state=("expanded" if node.expanded else "collapsed")
            if node.role == "ComboBox"
            else None,
            children=children,
        )

    # ---------------------------------------------------------------- helpers

    def _app_by_id(self, app_id: str) -> FakeApp:
        app = self.desktop.apps.get(app_id)
        if app is None or not app.alive:
            raise WindowGone(f"no running fake app {app_id!r}")
        return app

    def _app(self, handle: AppHandle) -> FakeApp:
        return self._app_by_id(handle.app_id)

    @staticmethod
    def _result(
        action: Action,
        started: float,
        resolved_by: str | None,
        element: Element | None,
        value: str | None,
        screenshot: Path | None,
    ) -> ActionResult:
        return ActionResult(
            ok=True,
            action_type=action.type,
            resolved_by=resolved_by,
            element=element,
            value=value,
            screenshot_path=screenshot,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
