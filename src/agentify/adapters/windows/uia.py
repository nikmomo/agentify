"""In-process UI Automation fast path for WinAppCliAdapter.

Every WinApp CLI call starts a process (about 150-330 ms, up to 1 s on large trees). Replay and
verification issue dozens of them. This module performs the hot operations through the UIA COM
API in-process: find (automation id, role + name, role + label, role + index, with the shared
selector semantics), invoke / select / toggle / expand / collapse, set and read values, names,
focus, scroll into view, wait_for, and real-input clicks.

Rules:
- Searches start only at the attached window and the windows it owns (dialogs, dropdown
  popups). Other top-level windows are never searched, even in the same process.
- Clicks activate the attached window first and send input only if the point belongs to one of
  those windows.
- One dedicated worker thread owns the COM client. A call that does not return in time (e.g. an
  invoke that opens a modal dialog) raises `UiaTimeout`, and the worker is replaced.
- Anything unsupported raises `UiaUnavailable`; the adapter then uses the CLI path.
"""

from __future__ import annotations

import ctypes
import queue
import threading
import time
from _ctypes import COMError
from collections.abc import Callable
from ctypes import wintypes
from typing import Any

from agentify.adapters.base.errors import AdapterTimeout, ElementNotFound, PermissionDenied
from agentify.adapters.base.models import Action, Element, Selector
from agentify.adapters.base.selectors import filter_matches, label_matches, pick, strategies

CONTROL_TYPES: dict[int, str] = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit",
    50005: "Hyperlink", 50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu",
    50010: "MenuBar", 50011: "MenuItem", 50012: "ProgressBar", 50013: "RadioButton",
    50014: "ScrollBar", 50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip", 50023: "Tree",
    50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb", 50028: "DataGrid",
    50029: "DataItem", 50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane",
    50034: "Header", 50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
    50039: "SemanticZoom", 50040: "AppBar",
}  # fmt: skip
ROLE_IDS = {v.casefold(): k for k, v in CONTROL_TYPES.items()}

# UIA property / pattern ids
P_NAME, P_AUTOMATION_ID, P_CONTROL_TYPE, P_OFFSCREEN = 30005, 30011, 30003, 30022
INVOKE, SELECTION, VALUE, EXPAND_COLLAPSE, SCROLL_ITEM, SELECTION_ITEM, TOGGLE = (
    10000, 10001, 10002, 10005, 10017, 10010, 10015,
)  # fmt: skip
TREE_DESCENDANTS = 4
PATTERN_TIMEOUT_S = 3.0  # invoke/select/toggle normally return at once; longer = stuck element
POST_ACTION_SETTLE_S = 0.15  # pause after an action that changes the UI (WinApp CLI took >= 0.3 s)
UI_CHANGING = frozenset({"invoke", "click", "set_value"})
EXPAND_STATES = {0: "Collapsed", 1: "Expanded", 2: "PartiallyExpanded", 3: "LeafNode"}

FAST_ACTIONS = frozenset(
    {
        "invoke",
        "set_value",
        "click",
        "focus",
        "scroll_into_view",
        "read_value",
        "locate",
        "wait_for",
    }
)


class UiaUnavailable(Exception):
    """The fast path cannot do this; use the CLI path."""


class UiaTimeout(AdapterTimeout):
    """A UIA call did not return in time (the action may have been performed). An infrastructure
    fault, not a skill defect: it never triggers a repair."""

    code = "backend_timeout"


# ---------------------------------------------------------------- the COM worker


class _Worker:
    """A thread that owns the UIA client and runs submitted functions in order."""

    def __init__(self) -> None:
        self._q: queue.Queue[tuple[Callable[[Any], Any], queue.Queue[tuple[bool, Any]]]] = (
            queue.Queue()
        )
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._main, name="agentify-uia", daemon=True)
        self.thread.start()

    def _main(self) -> None:
        try:
            import comtypes
            import comtypes.client

            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as uia_mod

            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            uia = comtypes.client.CreateObject(
                uia_mod.CUIAutomation, interface=uia_mod.IUIAutomation
            )
            ctx = _Ctx(uia, uia_mod)
        except BaseException as exc:
            self.error = exc
            self.ready.set()
            return
        self.ready.set()
        while True:
            fn, out = self._q.get()
            try:
                out.put((True, fn(ctx)))
            except BaseException as exc:
                out.put((False, exc))

    def submit(self, fn: Callable[[Any], Any], timeout_s: float) -> Any:
        out: queue.Queue[tuple[bool, Any]] = queue.Queue()
        self._q.put((fn, out))
        try:
            ok, value = out.get(timeout=timeout_s)
        except queue.Empty as exc:
            raise UiaTimeout(
                f"UI Automation call did not return within {timeout_s:.1f} s",
                details={"possibly_performed": True},
            ) from exc
        if not ok:
            raise value
        return value


class _Ctx:
    def __init__(self, uia: Any, mod: Any) -> None:
        self.uia = uia
        self.mod = mod
        cache = uia.CreateCacheRequest()
        for prop in (P_NAME, P_AUTOMATION_ID, P_CONTROL_TYPE, P_OFFSCREEN):
            cache.AddProperty(prop)
        self.cache = cache
        raw = uia.CreateCacheRequest()
        for prop in (P_NAME, P_AUTOMATION_ID, P_CONTROL_TYPE, P_OFFSCREEN):
            raw.AddProperty(prop)
        raw.TreeFilter = uia.RawViewCondition
        self.raw_cache = raw  # second look on a miss: window chrome is not in the control view
        self.true = uia.CreateTrueCondition()
        # "Real content": control types that window chrome (title bar, caption buttons) lacks.
        content = [
            uia.CreatePropertyCondition(P_CONTROL_TYPE, ROLE_IDS[r])
            for r in ("text", "edit", "document", "combobox", "listitem", "checkbox", "radiobutton",
                      "hyperlink", "tabitem", "treeitem", "dataitem")
        ]  # fmt: skip
        cond = content[0]
        for extra in content[1:]:
            cond = uia.CreateOrCondition(cond, extra)
        self.content = cond
        self.last_strategy = ""

    def pattern(self, el: Any, pattern_id: int, iface: str) -> Any:
        raw = el.GetCurrentPattern(pattern_id)
        if not raw:
            return None
        return raw.QueryInterface(getattr(self.mod, iface))


# ---------------------------------------------------------------- windows in scope


_user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None


def scope_windows(hwnd: int) -> list[int]:
    """The attached window, the visible top-level windows it owns (directly or transitively), and
    unowned popups of its UI thread (dropdown lists, menus). Other normal windows, even of the same
    process, are never included."""
    if _user32 is None:
        return [hwnd]
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetWindowLongW.restype = ctypes.c_long
    thread = _user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), None)
    found: list[int] = [hwnd]
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def owned_by_target(h: int) -> bool:
        owner = _user32.GetWindow(h, 4)  # GW_OWNER
        seen = 0
        while owner and seen < 10:
            if int(owner) == hwnd:
                return True
            owner = _user32.GetWindow(owner, 4)
            seen += 1
        return False

    def unowned_popup_of_thread(h: int) -> bool:
        style = _user32.GetWindowLongW(h, -16) & 0xFFFFFFFF
        return (
            bool(style & 0x80000000)  # WS_POPUP
            and not _user32.GetWindow(h, 4)
            and _user32.GetWindowThreadProcessId(h, None) == thread
        )

    def cb(h: int, _l: int) -> bool:
        if (
            h
            and int(h) != hwnd
            and _user32.IsWindowVisible(h)
            and (owned_by_target(h) or unowned_popup_of_thread(h))
        ):
            found.append(int(h))
        return True

    _user32.EnumWindows(proc(cb), 0)
    return found


def _root_of(h: int) -> int:
    assert _user32 is not None
    _user32.GetAncestor.restype = wintypes.HWND
    return int(_user32.GetAncestor(wintypes.HWND(h), 2) or 0)  # GA_ROOT


# ---------------------------------------------------------------- the fast path


class UiaFastPath:
    def __init__(self, *, call_timeout_s: float = 8.0, settle_ms: int = 1200) -> None:
        self.call_timeout_s = call_timeout_s
        self.settle_ms = settle_ms
        self._worker: _Worker | None = None
        self._lock = threading.Lock()

    # -- worker management
    def _run(self, fn: Callable[[Any], Any], timeout_s: float | None = None) -> Any:
        with self._lock:
            if self._worker is None:
                self._worker = _Worker()
            worker = self._worker
        worker.ready.wait(10)
        if worker.error is not None:
            raise UiaUnavailable(f"UI Automation unavailable: {worker.error}")
        try:
            return worker.submit(fn, timeout_s or self.call_timeout_s)
        except UiaTimeout:
            with self._lock:  # the worker is stuck in a call: start a fresh one next time
                if self._worker is worker:
                    self._worker = None
            raise

    # -- find
    def _find(self, c: _Ctx, hwnd: int, selector: Selector) -> list[tuple[Element, Any]]:
        roots = []
        for h in scope_windows(hwnd):
            try:
                roots.append(c.uia.ElementFromHandle(wintypes.HWND(h)))
            except COMError:
                if h == hwnd:
                    raise UiaUnavailable("the attached window is not available to UIA") from None
                # an owned window that is closing or not ready: skip it
        if selector.root is not None:
            matches = self._find(c, hwnd, selector.root)
            if not matches:
                return []
            root_el = pick([m[0] for m in matches], selector.root)
            roots = [next(com for e, com in matches if e is root_el)]
        for cache in (c.cache, c.raw_cache):
            found = self._find_in(c, roots, selector, cache)
            if found:
                return found
        return []

    def _find_in(
        self, c: _Ctx, roots: list[Any], selector: Selector, cache: Any
    ) -> list[tuple[Element, Any]]:
        for strategy in strategies(selector):
            s = strategy.selector
            if strategy.name == "slug":
                continue  # session slugs belong to the CLI
            if strategy.name == "automation_id":
                cond = c.uia.CreatePropertyCondition(P_AUTOMATION_ID, s.automation_id)
            elif s.role and s.role.casefold() in ROLE_IDS and strategy.name != "role_label":
                cond = c.uia.CreatePropertyCondition(P_CONTROL_TYPE, ROLE_IDS[s.role.casefold()])
            elif s.role and s.role.casefold() not in ROLE_IDS:
                raise UiaUnavailable(f"unknown role {s.role!r}")
            else:
                cond = c.true
            pairs: list[tuple[Element, Any]] = []
            seen: set[tuple[int, ...]] = set()
            for root in roots:
                arr = root.FindAllBuildCache(TREE_DESCENDANTS, cond, cache)
                for i in range(arr.Length if arr else 0):
                    el = arr.GetElement(i)
                    # An owned dialog can appear both under the main window and as its own root.
                    rid = tuple(el.GetRuntimeId() or ())
                    if rid and rid in seen:
                        continue
                    seen.add(rid)
                    pairs.append((_element(el), el))
            if strategy.name == "role_label":
                chosen = label_matches([p[0] for p in pairs], s)
            else:
                chosen = filter_matches([p[0] for p in pairs], s)
            if chosen:
                # Prefer what is on screen: a list that is closing can still expose its items.
                visible = [e for e in chosen if not e.offscreen]
                chosen = visible or chosen
                c.last_strategy = strategy.name
                ids = {id(e) for e in chosen}
                return [(e, com) for e, com in pairs if id(e) in ids]
        return []

    @staticmethod
    def _rendered(c: _Ctx, hwnd: int) -> bool:
        """The window shows real content (not an occluded WebView exposing only its chrome), so a
        miss here is a real miss and the slower CLI path need not look again."""
        root = c.uia.ElementFromHandle(wintypes.HWND(hwnd))
        return bool(root.FindFirst(TREE_DESCENDANTS, c.content))

    def find(self, hwnd: int, selector: Selector) -> tuple[list[Element], bool]:
        """(matches, trusted): an empty result is trusted when the window is rendered."""

        def go(c: _Ctx) -> tuple[list[Element], bool]:
            found = [e for e, _ in self._find(c, hwnd, selector)]
            return found, bool(found) or self._rendered(c, hwnd)

        result: tuple[list[Element], bool] = self._run(go)
        return result

    def _one(self, c: _Ctx, hwnd: int, selector: Selector) -> tuple[Element, Any]:
        pairs = self._find(c, hwnd, selector)
        if not pairs:
            raise ElementNotFound(
                f"no element matches ({selector.describe()})",
                details={"backend": "uia", "rendered": self._rendered(c, hwnd)},
            )
        el = pick([p[0] for p in pairs], selector)
        return next(p for p in pairs if p[0] is el)

    # -- reads
    def read(self, hwnd: int, selector: Selector, prop: str) -> Any:
        def go(c: _Ctx) -> Any:
            el, com = self._one(c, hwnd, selector)
            if prop in ("Name", "name"):
                return com.CurrentName
            if prop == "ExpandCollapseState":
                p = c.pattern(com, EXPAND_COLLAPSE, "IUIAutomationExpandCollapsePattern")
                return EXPAND_STATES.get(p.CurrentExpandCollapseState) if p else None
            if prop == "value":
                return _value(c, com, el)
            raise UiaUnavailable(f"property {prop!r}")

        return self._settled(go, self.call_timeout_s)

    def _settled(self, fn: Callable[[Any], Any], timeout_s: float) -> Any:
        """Run fn; while the element is missing from a rendered window, look again for up to
        `settle_ms`: UI that the previous action opened (a page, a dialog, a list) can appear a
        moment later. Elements that are there are found at once."""
        deadline = time.monotonic() + self.settle_ms / 1000
        while True:
            try:
                return self._run(fn, timeout_s)
            except ElementNotFound:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    # -- actions
    def perform(
        self, app_hwnd: int, app_pid: int, action: Action, timeout_ms: int
    ) -> tuple[Element | None, str | None, str]:
        """Returns (element, value read, resolved_by strategy name)."""
        kind = action.type
        if kind not in FAST_ACTIONS or action.selector is None:
            raise UiaUnavailable(kind)
        if kind == "wait_for":
            return self._wait_for(app_hwnd, action, timeout_ms), None, "wait_for"
        sel = action.selector

        def go(c: _Ctx) -> tuple[Element, str | None, tuple[int, int] | None, str]:
            el, com = self._one(c, app_hwnd, sel)
            if kind == "locate":
                return el, None, None, c.last_strategy
            if kind == "read_value":
                return el, _value(c, com, el), None, c.last_strategy
            if kind == "set_value":
                p = c.pattern(com, VALUE, "IUIAutomationValuePattern")
                if p is None or p.CurrentIsReadOnly:
                    raise UiaUnavailable("no writable ValuePattern")
                p.SetValue(action.value or "")
                return el, None, None, c.last_strategy
            if kind == "invoke":
                _invoke(c, com, action.pattern)
                return el, None, None, c.last_strategy
            if kind == "focus":
                com.SetFocus()
                return el, None, None, c.last_strategy
            if kind == "scroll_into_view":
                p = c.pattern(com, SCROLL_ITEM, "IUIAutomationScrollItemPattern")
                if p is None:
                    raise UiaUnavailable("no ScrollItemPattern")
                p.ScrollIntoView()
                return el, None, None, c.last_strategy
            if kind == "click":
                r = com.CurrentBoundingRectangle
                if r.right <= r.left or r.bottom <= r.top:
                    raise UiaUnavailable("element has no on-screen bounds")
                return el, None, ((r.left + r.right) // 2, (r.top + r.bottom) // 2), c.last_strategy
            raise UiaUnavailable(kind)

        pattern_call = kind == "invoke"
        try:
            element, value, point, how = self._settled(
                go, PATTERN_TIMEOUT_S if pattern_call else max(1.0, timeout_ms / 1000)
            )
        except UiaTimeout:
            if not (kind == "invoke" and action.pattern == "select"):
                raise
            # Selecting is idempotent, so a real click on the item is a safe second way.
            click = action.model_copy(update={"type": "click", "pattern": None})
            return self.perform(app_hwnd, app_pid, click, timeout_ms)
        if point is not None:
            self._click(app_hwnd, app_pid, point, action)
        if kind in UI_CHANGING:
            time.sleep(POST_ACTION_SETTLE_S)  # let the UI react before the next step looks at it
        return element, value, how

    def _wait_for(self, hwnd: int, action: Action, timeout_ms: int) -> Element | None:
        sel = action.selector
        assert sel is not None
        deadline = time.monotonic() + timeout_ms / 1000

        def probe(c: _Ctx) -> tuple[bool, Element | None]:
            pairs = self._find(c, hwnd, sel)
            if action.gone:
                return (not pairs), None
            for el, com in pairs:
                if action.value is None:
                    return True, el
                have = (
                    com.CurrentName
                    if action.property_name in ("Name", "name")
                    else _value(c, com, el)
                )
                have, want = str(have or ""), action.value
                if (want in have) if action.contains else (have == want):
                    return True, el
            return False, None

        while True:
            done, el = self._run(probe)
            if done:
                found: Element | None = el
                return found
            if time.monotonic() >= deadline:
                raise AdapterTimeout(
                    f"wait_for timed out after {timeout_ms} ms ({sel.describe()})",
                    details={"backend": "uia"},
                )
            time.sleep(0.05)

    @staticmethod
    def _click(hwnd: int, pid: int, point: tuple[int, int], action: Action) -> None:
        from agentify.adapters.windows.win32 import activate_window

        assert _user32 is not None
        activate_window(hwnd)
        old = _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        try:
            x, y = point
            _user32.WindowFromPoint.restype = wintypes.HWND
            under = _user32.WindowFromPoint(wintypes.POINT(x, y))
            top = _root_of(int(under or 0)) if under else 0
            # The top-level window under the point must be in scope. Its process is not compared:
            # web content (WebView2) is drawn by a child window of another process.
            if not under or top not in scope_windows(hwnd):
                raise PermissionDenied(
                    f"click point ({x},{y}) is not on the attached window (covered or off-screen)",
                    details={"point": [x, y]},
                )
            _user32.SetCursorPos(x, y)
            down, up = (0x0008, 0x0010) if action.button == "right" else (0x0002, 0x0004)
            for _ in range(2 if action.double else 1):
                _user32.mouse_event(down, 0, 0, 0, 0)
                _user32.mouse_event(up, 0, 0, 0, 0)
        finally:
            _user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(old or -4))


def _element(com: Any) -> Element:
    role = CONTROL_TYPES.get(int(com.CachedControlType), "Custom")
    aid = com.CachedAutomationId or None
    try:
        offscreen = bool(com.GetCachedPropertyValue(P_OFFSCREEN))
    except Exception:
        offscreen = False
    return Element(role=role, name=com.CachedName or None, automation_id=aid, offscreen=offscreen)


def _value(c: _Ctx, com: Any, el: Element) -> str | None:
    p = c.pattern(com, VALUE, "IUIAutomationValuePattern")
    if p is not None:
        return str(p.CurrentValue)
    sel = c.pattern(com, SELECTION, "IUIAutomationSelectionPattern")
    if sel is not None:
        items = sel.GetCurrentSelection()
        return items.GetElement(0).CurrentName if items and items.Length else None
    raise UiaUnavailable(f"no value pattern on {el.role}")


def _invoke(c: _Ctx, com: Any, pattern: str | None) -> None:
    if pattern in (None, "invoke"):
        p = c.pattern(com, INVOKE, "IUIAutomationInvokePattern")
        if p is None:
            raise UiaUnavailable("no InvokePattern")
        p.Invoke()
    elif pattern == "select":
        p = c.pattern(com, SELECTION_ITEM, "IUIAutomationSelectionItemPattern")
        if p is None:
            raise UiaUnavailable("no SelectionItemPattern")
        p.Select()
    elif pattern in ("toggle", "toggle-on", "toggle-off"):
        p = c.pattern(com, TOGGLE, "IUIAutomationTogglePattern")
        if p is None:
            raise UiaUnavailable("no TogglePattern")
        want = {"toggle-on": 1, "toggle-off": 0}.get(pattern)
        for _ in range(3):
            if want is not None and p.CurrentToggleState == want:
                break
            p.Toggle()
            if want is None:
                break
    elif pattern in ("expand", "collapse"):
        p = c.pattern(com, EXPAND_COLLAPSE, "IUIAutomationExpandCollapsePattern")
        if p is None:
            raise UiaUnavailable("no ExpandCollapsePattern")
        p.Expand() if pattern == "expand" else p.Collapse()
    else:
        raise UiaUnavailable(f"pattern {pattern!r}")


# ---------------------------------------------------------------- keyboard (SendInput)

_VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "enter": 0x0D, "return": 0x0D, "tab": 0x09,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "backspace": 0x08, "del": 0x2E, "delete": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "up": 0x26,
    "down": 0x28, "left": 0x25, "right": 0x27, "control": 0x11, "ctl": 0x11,
}  # fmt: skip
_MODIFIERS = {0x10, 0x11, 0x12}


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _vk(name: str) -> int:
    low = name.lower()
    if low in _VK:
        return _VK[low]
    if len(low) == 1 and (low.isalpha() or low.isdigit()):
        return ord(low.upper())
    if low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
        return 0x6F + int(low[1:])
    if low.startswith("vk="):
        return int(low[3:], 0)
    raise UiaUnavailable(f"key {name!r}")


def _unescape(t: str) -> str:
    escapes = {"s": " ", "t": "\t", "n": "\n", "r": "\r", "\\": "\\"}
    out, i = [], 0
    while i < len(t):
        if t[i] == "\\" and i + 1 < len(t) and t[i + 1] in escapes:
            out.append(escapes[t[i + 1]])
            i += 2
        else:
            out.append(t[i])
            i += 1
    return "".join(out)


def _combo(token: str) -> list[int] | None:
    """Virtual keys of a named key or combo ("enter", "ctrl+shift+t"), or None for literal text."""
    parts = [p for p in token.split("+") if p]
    try:
        codes = [_vk(p) for p in parts]
    except UiaUnavailable:
        return None
    if len(parts) == 1 and len(parts[0]) == 1:
        return None  # a single character is typed as text
    return codes


def key_events(keys: str, *, verbatim: bool) -> list[tuple[int, int, int]]:
    """(vk, scan, flags) events for WinApp CLI's key syntax: whitespace-separated tokens, each a
    named key or combo ("enter", "ctrl+s"), "vk=0xNN", "text=<literal>" (escapes \\s \\t \\n
    \\r \\\\), or any other word typed literally; verbatim = the whole string typed as is."""
    up, uni = 0x0002, 0x0004
    events: list[tuple[int, int, int]] = []

    def text(t: str) -> None:
        for ch in t:
            if ch in "\r\n":
                events.extend([(0x0D, 0, 0), (0x0D, 0, up)])
                continue
            if ch == "\t":
                events.extend([(0x09, 0, 0), (0x09, 0, up)])
                continue
            code = ord(ch)
            if code > 0xFFFF:
                raise UiaUnavailable("characters outside the BMP")
            events.extend([(0, code, uni), (0, code, uni | up)])

    if verbatim:
        text(keys)
        return events
    for token in keys.split():
        low = token.lower()
        if low.startswith("text="):
            text(_unescape(token[5:]))
            continue
        if low.startswith("vk="):
            code = _vk(token)
            events.extend([(code, 0, 0), (code, 0, up)])
            continue
        codes = _combo(token)
        if codes is None:
            text(token)
            continue
        mods = [c for c in codes if c in _MODIFIERS]
        main = [c for c in codes if c not in _MODIFIERS]
        events.extend((c, 0, 0) for c in mods)
        for c in main:
            events.extend([(c, 0, 0), (c, 0, up)])
        events.extend((c, 0, up) for c in reversed(mods))
    return events


def send_keys(hwnd: int, keys: str, *, verbatim: bool) -> None:
    """Type into the attached window with SendInput, only while it (or a window it owns) is in the
    foreground. The caller has already applied the key deny list (adapters/base/keys.py)."""
    from agentify.adapters.windows.win32 import activate_window

    assert _user32 is not None
    events = key_events(keys, verbatim=verbatim)  # parse first: unknown keys -> CLI path
    activate_window(hwnd)
    _user32.GetForegroundWindow.restype = wintypes.HWND
    fg = _user32.GetForegroundWindow()
    if not fg or _root_of(int(fg)) not in scope_windows(hwnd):
        raise UiaUnavailable("the attached window is not in the foreground")
    arr = (_INPUT * len(events))()
    for i, (vk, scan, flags) in enumerate(events):
        arr[i].type = 1  # INPUT_KEYBOARD
        arr[i].ki = _KEYBDINPUT(vk, scan, flags, 0, None)
    sent = _user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):
        raise UiaUnavailable("SendInput was blocked")
