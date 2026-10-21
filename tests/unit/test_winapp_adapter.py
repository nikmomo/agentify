"""WinAppCliAdapter logic against recorded WinApp CLI 0.7.1 payloads (no desktop required)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentify.adapters.base import (
    Action,
    ActionNotSupported,
    AdapterTimeout,
    AmbiguousSelector,
    ElementNotFound,
    InvalidAction,
    PermissionDenied,
    Selector,
    WindowGone,
)
from agentify.adapters.windows import WinAppCliAdapter
from agentify.adapters.windows.parse import parse_app_id
from agentify.adapters.windows.runner import redact_args

from .conftest import ScriptedRunner

pytestmark = pytest.mark.unit


def make(runner: ScriptedRunner, tmp_path: Path | None = None) -> WinAppCliAdapter:
    return WinAppCliAdapter(
        runner,
        screenshots_dir=tmp_path or Path("."),
        enforce_process_scope=False,
        warm_accessibility=False,
        activator=lambda hwnd: True,  # never touch real windows from unit tests
        activate_for_input=False,
    )


def test_attach_warms_accessibility_tree(runner: ScriptedRunner) -> None:
    runner.on("status", "status").on("inspect", "inspect")
    adapter = WinAppCliAdapter(runner, enforce_process_scope=False)
    adapter.attach("powershell:394880")
    assert runner.commands() == ["status", "inspect"]
    assert runner.calls[-1][:3] == ["inspect", "-w", "394880"]


def test_attach_tolerates_warmup_failure(runner: ScriptedRunner) -> None:
    runner.on("status", "status").on("inspect", "err_bad_hwnd")
    adapter = WinAppCliAdapter(runner, enforce_process_scope=False)
    assert adapter.attach("powershell:394880").hwnd == 394880


def attached(runner: ScriptedRunner) -> tuple[WinAppCliAdapter, object]:
    runner.on("status", "status")
    adapter = make(runner)
    return adapter, adapter.attach("powershell:394880")


def test_list_applications_filters_shell_windows(runner: ScriptedRunner) -> None:
    runner.on("list-windows", "list_windows")
    apps = make(runner).list_applications()
    assert [a.app_id for a in apps] == ["powershell:394880"]
    assert apps[0].pid == 10328 and apps[0].is_foreground
    assert runner.calls[0] == ["list-windows", "--json"]


def test_attach_reads_dpi_and_checks_process(runner: ScriptedRunner) -> None:
    runner.on("status", "status")
    adapter = make(runner)
    handle = adapter.attach("powershell:394880")
    assert handle.hwnd == 394880 and handle.pid == 10328 and handle.dpi_scale == 1.0
    with pytest.raises(WindowGone):
        adapter.attach("notepad:394880")
    with pytest.raises(InvalidAction):
        adapter.attach("not-an-id")


def test_attach_missing_window_maps_to_window_gone(runner: ScriptedRunner) -> None:
    runner.on("status", "err_bad_hwnd")
    with pytest.raises(WindowGone):
        make(runner).attach("123")


def test_parse_app_id() -> None:
    assert parse_app_id("my-app:1509170") == ("my-app", 1509170)
    assert parse_app_id("42") == (None, 42)


def test_observe_parses_tree(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("inspect", "inspect")
    obs = adapter.observe(app)  # type: ignore[arg-type]
    # Default: deep, unfiltered tree (the CLI's --interactive filter prunes WebView2 content).
    assert "--interactive" not in runner.calls[-1]
    assert runner.calls[-1][runner.calls[-1].index("--depth") + 1] == "30"
    adapter.observe(app, interactive_only=True, depth=8)  # type: ignore[arg-type]
    assert runner.calls[-1][-1] == "--interactive"
    by_id = {e.automation_id: e for e in obs.iter_elements() if e.automation_id}
    assert by_id["ColourCombo"].value == "Red" and by_id["ColourCombo"].has_stable_automation_id
    assert not by_id["788468"].has_stable_automation_id
    open_btn = next(e for e in obs.iter_elements() if e.name == "Open")
    assert open_btn.invokable and open_btn.slug == "btn-open-0011"
    menu = next(e for e in obs.iter_elements() if e.role == "MenuItem")
    assert menu.expand_state == "collapsed" and menu.ancestor_path == (
        "Window",
        "TitleBar",
        "MenuBar",
    )
    assert obs.windows[0].info.dpi_awareness == "unaware"


def test_role_name_resolution_disambiguates(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("search", "search_colour")
    resolved = adapter.resolve(app, Selector(role="ComboBox", name="Colour"))  # type: ignore[arg-type]
    assert resolved.cli_selector == "ColourCombo" and resolved.resolved_by == "role_name"
    assert runner.calls[-1][:2] == ["search", "Colour"] and "--type" in runner.calls[-1]
    with pytest.raises(AmbiguousSelector):
        adapter.resolve(app, Selector(name="Colour"))  # type: ignore[arg-type]


def test_automation_id_strategy_requires_exact_id(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("search", "search_colour")
    resolved = adapter.resolve(
        app, Selector(automation_id="ColourCombo", role="ComboBox", name="Colour")
    )  # type: ignore[arg-type]
    assert resolved.resolved_by == "automation_id" and resolved.element.role == "ComboBox"


def test_fallback_from_missing_automation_id_to_role_name(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("search", "search_empty", "OldId").on("search", "search_colour", "Colour")
    resolved = adapter.resolve(app, Selector(automation_id="OldId", role="ComboBox", name="Colour"))  # type: ignore[arg-type]
    assert resolved.resolved_by == "role_name"


def test_not_found_lists_strategies(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("search", "search_empty")
    with pytest.raises(ElementNotFound) as info:
        adapter.resolve(app, Selector(automation_id="X", name="Y"))  # type: ignore[arg-type]
    assert info.value.details["strategies_tried"] == ["automation_id", "role_name"]


def test_invoke_select_uses_resolved_slug_and_pattern(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("search", "search_blue_item").on("invoke", "invoke_select")
    result = adapter.perform(
        app,
        Action(
            type="invoke",
            pattern="select",
            selector=Selector(role="ListItem", name="Blue", exact=True),
        ),
    )  # type: ignore[arg-type]
    assert runner.calls[-1] == [
        "invoke",
        "itm-blue-8123",
        "-w",
        "394880",
        "--json",
        "--action",
        "select",
    ]
    assert result.ok and result.resolved_by == "role_name" and result.target_hwnd == 3080458


def test_unsupported_pattern_maps_to_action_not_supported(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("search", "search_colour").on("invoke", "err_no_expand")
    with pytest.raises(ActionNotSupported):
        adapter.perform(
            app,
            Action(type="invoke", pattern="expand", selector=Selector(automation_id="ColourCombo")),
        )  # type: ignore[arg-type]


def test_set_value_and_read_value(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("search", "search_colour").on("set-value", "set_value").on(
        "get-value", "get_value_combo"
    )
    adapter.perform(
        app, Action(type="set_value", selector=Selector(automation_id="ColourCombo"), value="Blue")
    )  # type: ignore[arg-type]
    assert runner.calls[-1][:3] == ["set-value", "ColourCombo", "Blue"]
    assert adapter.read_value(app, Selector(automation_id="ColourCombo")) == "Blue"  # type: ignore[arg-type]


def test_send_keys_defaults_to_send_input_and_blocks_system_keys(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("search", "search_colour").on("send-keys", "send_keys")
    adapter.perform(
        app,
        Action(type="send_keys", keys="text=hi", selector=Selector(automation_id="ColourCombo")),
    )  # type: ignore[arg-type]
    last = runner.calls[-1]
    assert (
        last[last.index("--via") + 1] == "send-input"
        and last[last.index("--target") + 1] == "ColourCombo"
    )
    n = len(runner.calls)
    with pytest.raises(PermissionDenied):
        adapter.perform(app, Action(type="send_keys", keys="alt+f4"))  # type: ignore[arg-type]
    assert len(runner.calls) == n, "blocked keys must never reach the CLI"


def test_wait_for_found_and_timeout(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("wait-for", "wait_for_found", times=1).on("wait-for", "wait_for_timeout")
    ok = adapter.perform(
        app,
        Action(
            type="wait_for",
            selector=Selector(automation_id="StatusLabel"),
            value="Current colour: Blue",
            property_name="Name",
        ),
    )  # type: ignore[arg-type]
    assert ok.element is not None and ok.element.name == "Current colour: Blue"
    assert "--property" in runner.calls[-1] and "Name" in runner.calls[-1]
    with pytest.raises(AdapterTimeout):
        adapter.perform(
            app, Action(type="wait_for", selector=Selector(name="Nope"), timeout_ms=1000)
        )  # type: ignore[arg-type]


def test_cli_invalid_arguments_maps_to_invalid_action(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("search", "search_colour").on("click", "err_invalid_args")
    with pytest.raises(InvalidAction):
        adapter.perform(app, Action(type="click", selector=Selector(automation_id="ColourCombo")))  # type: ignore[arg-type]


def test_screenshot_and_get_property(runner: ScriptedRunner, tmp_path: Path) -> None:
    runner.on("status", "status").on("screenshot", "screenshot").on("search", "search_colour").on(
        "get-property", "get_property"
    )
    adapter = make(runner, tmp_path)
    app = adapter.attach("powershell:394880")
    assert adapter.capture_screenshot(app) == Path("C:\\tmp\\fx.png")
    assert (
        adapter.get_property(app, Selector(automation_id="StatusLabel"), "Name")
        == "Current colour: Red"
    )


def test_read_state_alive_and_gone(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("list-windows", "list_windows")
    assert adapter.read_state(app).alive  # type: ignore[arg-type]
    gone = app.model_copy(update={"hwnd": 1})  # type: ignore[attr-defined]
    assert not adapter.read_state(gone).alive


def test_redact_args() -> None:
    assert redact_args(["set-value", "NotesBox", "secret", "-w", "1"]) == [
        "set-value",
        "NotesBox",
        "<6 chars>",
        "-w",
        "1",
    ]
    assert redact_args(["send-keys", "text=pw enter"])[1] == "text=<2 chars> enter"


def test_stale_slug_falls_back_to_role_name(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("search", "err_stale", "cmb-colourcombo-0000").on("search", "search_colour", "Colour")
    sel = Selector(slug="cmb-colourcombo-0000", role="ComboBox", name="Colour", exact=True)
    assert adapter.resolve(app, sel).resolved_by == "role_name"  # type: ignore[arg-type]


def test_stale_error_is_typed() -> None:
    from agentify.adapters.base import StaleSelector
    from agentify.adapters.windows.runner import CliCall, map_error

    from .conftest import recorded

    err = recorded("err_stale")["stderr"]["error"]
    mapped = map_error(
        CliCall(args=["invoke", "x"], exit_code=1, duration_ms=1, stdout="", stderr="", error=err)
    )
    assert isinstance(mapped, StaleSelector) and isinstance(mapped, ElementNotFound)


def test_direct_hint_skips_lookup_for_stable_automation_id(runner: ScriptedRunner) -> None:
    """Replay fast path: act on the persisted automation id without a separate search call."""
    adapter, app = attached(runner)
    runner.on("set-value", "set_value")
    n = len(runner.calls)
    r = adapter.perform(
        app,
        Action(
            type="set_value",
            selector=Selector(automation_id="ColourCombo", role="ComboBox", name="Colour"),
            value="Blue",
            direct=True,
        ),
    )  # type: ignore[arg-type]
    assert runner.commands()[n:] == ["set-value"]
    assert runner.calls[-1][:3] == ["set-value", "ColourCombo", "Blue"]
    assert r.resolved_by == "automation_id_direct"


def test_direct_hint_falls_back_to_resolution_when_not_found(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("set-value", "err_stale", times=1).on("search", "search_colour").on(
        "set-value", "set_value"
    )
    n = len(runner.calls)
    r = adapter.perform(
        app,
        Action(
            type="set_value",
            selector=Selector(automation_id="ColourCombo", role="ComboBox", name="Colour"),
            value="Blue",
            direct=True,
        ),
    )  # type: ignore[arg-type]
    assert runner.commands()[n:] == ["set-value", "search", "set-value"]
    assert r.resolved_by == "automation_id"


def test_direct_hint_ignored_for_numeric_ids(runner: ScriptedRunner) -> None:
    adapter, app = attached(runner)
    runner.on("search", "search_colour").on("set-value", "set_value")
    n = len(runner.calls)
    with pytest.raises(
        AmbiguousSelector
    ):  # recorded search has several matches; we only check the path
        adapter.perform(
            app,
            Action(
                type="set_value",
                selector=Selector(automation_id="788468", name="Colour"),
                value="x",
                direct=True,
            ),
        )  # type: ignore[arg-type]
    cmds = runner.commands()[n:]
    assert cmds[0] == "search" and "set-value" not in cmds, (
        "per-launch numeric ids are never trusted"
    )


def test_real_input_activates_only_the_attached_window(runner: ScriptedRunner) -> None:
    runner.on("status", "status").on("search", "search_colour").on("click", "set_value").on(
        "set-value", "set_value"
    ).on("send-keys", "send_keys")
    activated: list[int] = []
    adapter = WinAppCliAdapter(
        runner,
        enforce_process_scope=False,
        warm_accessibility=False,
        activator=lambda hwnd: activated.append(hwnd) or True,
    )
    app = adapter.attach("powershell:394880")
    sel = Selector(automation_id="ColourCombo")
    adapter.perform(app, Action(type="set_value", selector=sel, value="Blue"))
    assert activated == [], "pattern-based actions do not need the foreground"
    adapter.perform(app, Action(type="click", selector=sel))
    adapter.perform(app, Action(type="send_keys", keys="text=hi", selector=sel))
    assert activated == [394880, 394880]
