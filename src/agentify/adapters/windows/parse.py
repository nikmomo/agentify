"""Parse WinApp CLI 0.7.1 JSON payloads into adapter models. Lenient: unknown fields are ignored."""

from __future__ import annotations

from typing import Any

from agentify.adapters.base.models import (
    AppInfo,
    Bounds,
    Element,
    Observation,
    ObservedWindow,
    WindowInfo,
)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value)
    return s if s != "" else None


def parse_bounds(d: dict[str, Any]) -> Bounds | None:
    try:
        return Bounds(x=int(d["x"]), y=int(d["y"]), width=int(d["width"]), height=int(d["height"]))
    except (KeyError, TypeError, ValueError):
        return None


def parse_element(d: dict[str, Any]) -> Element:
    return Element(
        role=str(d.get("type") or "Unknown"),
        name=_opt_str(d.get("name")),
        automation_id=_opt_str(d.get("automationId")),
        class_name=_opt_str(d.get("className")),
        slug=_opt_str(d.get("selector")),
        value=_opt_str(d.get("value")),
        bounds=parse_bounds(d),
        enabled=bool(d.get("isEnabled", True)),
        offscreen=bool(d.get("isOffscreen", False)),
        invokable=bool(d.get("isInvokable", False)),
        expand_state=_opt_str(d.get("expandState")),
        ancestor_path=tuple(str(a) for a in d.get("ancestorPath") or ()),
        children=tuple(parse_element(c) for c in d.get("children") or () if isinstance(c, dict)),
        has_more_children=bool(d.get("hasMoreChildren", False)),
    )


def parse_window_info(d: dict[str, Any]) -> WindowInfo:
    return WindowInfo(
        hwnd=int(d.get("hwnd", 0)),
        title=str(d.get("title") or d.get("windowTitle") or ""),
        class_name=_opt_str(d.get("className")),
        dpi=int(d["windowDpi"]) if d.get("windowDpi") is not None else None,
        scale=float(d["scale"]) if d.get("scale") is not None else None,
        dpi_awareness=_opt_str(d.get("dpiAwareness")),
        coordinate_space=_opt_str(d.get("coordinateSpace")),
    )


def parse_observation(app_id: str, payload: dict[str, Any]) -> Observation:
    windows = []
    for w in payload.get("windows") or ():
        windows.append(
            ObservedWindow(
                info=parse_window_info(w),
                elements=tuple(parse_element(e) for e in w.get("elements") or ()),
                element_count=w.get("elementCount"),
            )
        )
    return Observation(app_id=app_id, windows=tuple(windows))


def make_app_id(process_name: str, hwnd: int) -> str:
    return f"{process_name}:{hwnd}"


def parse_app_id(app_id: str) -> tuple[str | None, int]:
    """`process:hwnd` or a bare hwnd. Raises ValueError otherwise."""
    if ":" in app_id:
        proc, _, hwnd = app_id.rpartition(":")
        return (proc or None), int(hwnd)
    return None, int(app_id)


def parse_app_info(d: dict[str, Any]) -> AppInfo:
    proc = str(d.get("processName") or "unknown")
    hwnd = int(d.get("hwnd", 0))
    owner = d.get("ownerHwnd")
    return AppInfo(
        app_id=make_app_id(proc, hwnd),
        platform="windows",
        process_name=proc,
        pid=int(d.get("processId", 0)),
        hwnd=hwnd,
        window_title=str(d.get("title") or ""),
        window_class=_opt_str(d.get("className")),
        width=d.get("width"),
        height=d.get("height"),
        owner_hwnd=int(owner) if owner else None,
        is_foreground=bool(d.get("isForeground", False)),
    )


def parse_search(payload: Any) -> list[Element]:
    if not isinstance(payload, dict):
        return []
    return [parse_element(m) for m in payload.get("matches") or () if isinstance(m, dict)]
