"""Real WinApp CLI 0.7.1 against the WinForms UI-change fixture. Requires an unlocked desktop.

uv run pytest -m integration
"""

from __future__ import annotations

import time

import pytest

from agentify.adapters.base import (
    Action,
    ActionNotSupported,
    AdapterTimeout,
    AmbiguousSelector,
    ElementNotFound,
    PermissionDenied,
    Selector,
)
from agentify.adapters.windows import WinAppCliAdapter

from .conftest import RunningFixture

pytestmark = [pytest.mark.integration, pytest.mark.windows]

COMBO = Selector(automation_id="ColourCombo", role="ComboBox", name="Colour")
NOTES = Selector(automation_id="NotesBox", role="Edit", name="Notes")


def test_discover_attach_and_state(fixture_a: RunningFixture) -> None:
    a, app = fixture_a.adapter, fixture_a.app
    assert app.process_name.lower() == "powershell" and app.pid == fixture_a.proc.pid
    assert app.dpi_scale is not None
    state = a.read_state(app)
    assert state.alive and state.window_title == "Agentify Fixture vA"


def test_observe_tree(fixture_a: RunningFixture) -> None:
    obs = fixture_a.adapter.observe(fixture_a.app, interactive_only=False)
    elements = list(obs.iter_elements())
    combo = next(e for e in elements if e.automation_id == "ColourCombo")
    assert combo.role == "ComboBox" and combo.value == "Red" and combo.has_stable_automation_id
    label = next(e for e in elements if e.role == "Text" and e.name == "Colour")
    assert not label.has_stable_automation_id, "WinForms label ids are HWND-derived"
    assert any(e.role == "Button" and e.name == "Save" for e in elements)


def test_set_value_and_read_back(fixture_a: RunningFixture) -> None:
    a, app = fixture_a.adapter, fixture_a.app
    r = a.perform(app, Action(type="set_value", selector=NOTES, value="hello agentify"))
    assert r.resolved_by == "automation_id"
    assert a.read_value(app, NOTES) == "hello agentify"


def test_send_keys_send_input_appends_text(fixture_a: RunningFixture) -> None:
    a, app = fixture_a.adapter, fixture_a.app
    a.perform(app, Action(type="set_value", selector=NOTES, value="abc"))
    a.perform(app, Action(type="send_keys", keys="end text=\\sdef", selector=NOTES))
    assert a.read_value(app, NOTES) == "abc def"


def test_combobox_expand_pattern_unsupported_but_open_button_works(
    fixture_a: RunningFixture,
) -> None:
    a, app = fixture_a.adapter, fixture_a.app
    with pytest.raises(ActionNotSupported):
        a.perform(app, Action(type="invoke", pattern="expand", selector=COMBO))
    a.perform(
        app,
        Action(
            type="invoke", selector=Selector(role="Button", name="Open", exact=True, root=COMBO)
        ),
    )
    a.perform(
        app,
        Action(
            type="wait_for",
            selector=Selector(role="ListItem", name="Blue", exact=True),
            timeout_ms=3000,
        ),
    )
    r = a.perform(
        app,
        Action(
            type="invoke",
            pattern="select",
            selector=Selector(role="ListItem", name="Blue", exact=True),
        ),
    )
    assert r.target_hwnd != app.hwnd, "dropdown items live in a popup window of the same process"
    assert a.read_value(app, COMBO) == "Blue"


def test_combobox_set_value(fixture_a: RunningFixture) -> None:
    a, app = fixture_a.adapter, fixture_a.app
    a.perform(app, Action(type="set_value", selector=COMBO, value="Green"))
    assert a.read_value(app, COMBO) == "Green"


def test_save_writes_file_and_label_updates(fixture_a: RunningFixture) -> None:
    a, app = fixture_a.adapter, fixture_a.app
    a.perform(app, Action(type="set_value", selector=COMBO, value="Green"))
    a.perform(app, Action(type="set_value", selector=NOTES, value="n1"))
    a.perform(
        app,
        Action(
            type="invoke",
            selector=Selector(role="Button", name="Save", exact=True),
            idempotent=False,
        ),
    )
    a.perform(
        app,
        Action(
            type="wait_for",
            selector=Selector(automation_id="StatusLabel"),
            value="Current colour: Green",
            property_name="Name",
            timeout_ms=3000,
        ),
    )
    assert fixture_a.profile() == {"version": "A", "colour": "Green", "notes": "n1"}


def test_selector_errors(fixture_a: RunningFixture) -> None:
    a, app = fixture_a.adapter, fixture_a.app
    with pytest.raises(AmbiguousSelector):
        a.resolve(app, Selector(name="Colour"))
    assert (
        a.resolve(app, Selector(role="ComboBox", name="Colour")).element.automation_id
        == "ColourCombo"
    )
    with pytest.raises(ElementNotFound):
        a.resolve(app, Selector(name="DefinitelyNotThere"))
    stale = Selector(slug="cmb-colourcombo-0000", role="ComboBox", name="Colour", exact=True)
    assert a.resolve(app, stale).resolved_by == "role_name", (
        "stale slug must fall back to role+name"
    )


def test_wait_for_timeout_is_bounded(fixture_a: RunningFixture) -> None:
    started = time.perf_counter()
    with pytest.raises(AdapterTimeout):
        fixture_a.adapter.perform(
            fixture_a.app, Action(type="wait_for", selector=Selector(name="Never"), timeout_ms=800)
        )
    assert time.perf_counter() - started < 15


def test_system_keys_never_reach_the_window(fixture_a: RunningFixture) -> None:
    a, app = fixture_a.adapter, fixture_a.app
    with pytest.raises(PermissionDenied):
        a.perform(app, Action(type="send_keys", keys="alt+f4"))
    assert a.read_state(app).alive


def test_screenshot(fixture_a: RunningFixture) -> None:
    path = fixture_a.adapter.capture_screenshot(fixture_a.app)
    assert path.exists() and path.stat().st_size > 0


def test_close_button_ends_app_and_state_reports_gone(fixture_a: RunningFixture) -> None:
    a, app = fixture_a.adapter, fixture_a.app
    a.perform(app, Action(type="invoke", selector=Selector(automation_id="Close", role="Button")))
    fixture_a.proc.wait(timeout=10)
    assert not a.read_state(app).alive


def test_version_b_dialog_flow(fixture_b: RunningFixture) -> None:
    """Owned dialog windows must be reachable while scoped to the main window's hwnd."""
    a, app = fixture_b.adapter, fixture_b.app
    with pytest.raises(ElementNotFound):
        a.resolve(app, COMBO)
    # UIA Invoke blocks while a modal dialog is open, so a modal-opening button is clicked.
    a.perform(
        app,
        Action(
            type="click",
            selector=Selector(automation_id="PreferencesButton", role="Button", name="Preferences"),
        ),
    )
    a.perform(
        app,
        Action(
            type="wait_for",
            selector=Selector(role="RadioButton", name="Blue", exact=True),
            timeout_ms=5000,
        ),
    )
    a.perform(
        app,
        Action(
            type="invoke",
            pattern="select",
            selector=Selector(role="RadioButton", name="Blue", exact=True),
        ),
    )
    a.perform(
        app,
        Action(
            type="invoke",
            selector=Selector(role="Button", name="Apply", exact=True),
            idempotent=False,
        ),
    )
    a.perform(
        app,
        Action(
            type="wait_for",
            selector=Selector(role="Button", name="Apply", exact=True),
            gone=True,
            timeout_ms=5000,
        ),
    )
    assert fixture_b.profile()["colour"] == "Blue"


def test_slugs_are_session_scoped(adapter: WinAppCliAdapter) -> None:
    """Record whether semantic slugs survive a relaunch (they did not on 2026-10-15)."""
    from .conftest import _launch

    def label_slug() -> str | None:
        gen = _launch(adapter, "A")
        fx = next(gen)
        try:
            obs = adapter.observe(fx.app, interactive_only=False)
            combo = next(e for e in obs.iter_elements() if e.automation_id == "ColourCombo")
            return next((c.slug for c in combo.children if c.role == "Text"), None)
        finally:
            gen.close()

    first, second = label_slug(), label_slug()
    assert first and second
    print(f"slug launch1={first} launch2={second} stable={first == second}")
