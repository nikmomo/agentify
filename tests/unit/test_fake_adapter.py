from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentify.adapters import create_adapter, platform_support
from agentify.adapters.base import (
    Action,
    ActionNotSupported,
    AdapterTimeout,
    AmbiguousSelector,
    ElementNotFound,
    PermissionDenied,
    PlatformAdapter,
    Selector,
    UnsupportedPlatformError,
    WindowGone,
)
from agentify.adapters.fake import FakeAdapter, FakeDesktop, colour_fixture_app, simple_editor_app
from agentify.adapters.unsupported import BrowserAdapter, LinuxAdapter, MacOSAdapter
from agentify.config import load_settings

pytestmark = pytest.mark.unit

COMBO = Selector(automation_id="ColourCombo", role="ComboBox", name="Colour")


@pytest.fixture
def desktop() -> FakeDesktop:
    return FakeDesktop()


def test_fake_and_windows_satisfy_protocol(tmp_path: Path) -> None:
    assert isinstance(FakeAdapter(), PlatformAdapter)
    from agentify.adapters.windows import WinAppCliAdapter

    assert isinstance(WinAppCliAdapter(screenshots_dir=tmp_path), PlatformAdapter)


def test_version_a_learnable_flow(desktop: FakeDesktop, tmp_path: Path) -> None:
    fake = colour_fixture_app(desktop, version="A", state_file=tmp_path / "profile.json")
    adapter = FakeAdapter(desktop)
    app = adapter.attach(fake.app_id)
    assert [a.app_id for a in adapter.list_applications()] == [fake.app_id]

    with pytest.raises(ElementNotFound):
        adapter.perform(app, Action(type="invoke", selector=Selector(role="ListItem", name="Blue")))
    adapter.perform(app, Action(type="invoke", pattern="expand", selector=COMBO))
    r = adapter.perform(
        app,
        Action(
            type="invoke",
            pattern="select",
            selector=Selector(role="ListItem", name="Blue", exact=True),
        ),
    )
    assert r.resolved_by == "role_name"
    assert adapter.read_value(app, COMBO) == "Blue"
    adapter.perform(
        app, Action(type="set_value", selector=Selector(automation_id="NotesBox"), value="hi")
    )
    adapter.perform(
        app,
        Action(type="send_keys", keys="text=\\sthere", selector=Selector(automation_id="NotesBox")),
    )
    adapter.perform(app, Action(type="invoke", selector=Selector(role="Button", name="Save")))
    adapter.perform(
        app,
        Action(
            type="wait_for",
            selector=Selector(automation_id="StatusLabel"),
            value="Blue",
            property_name="Name",
            contains=True,
        ),
    )
    assert json.loads((tmp_path / "profile.json").read_text()) == {
        "colour": "Blue",
        "notes": "hi there",
        "version": "A",
    }


def test_version_b_breaks_version_a_selectors(desktop: FakeDesktop, tmp_path: Path) -> None:
    fake = colour_fixture_app(desktop, version="B", state_file=tmp_path / "profile.json")
    adapter = FakeAdapter(desktop)
    app = adapter.attach(fake.app_id)
    with pytest.raises(ElementNotFound):
        adapter.perform(app, Action(type="invoke", pattern="expand", selector=COMBO))
    adapter.perform(
        app, Action(type="invoke", selector=Selector(role="Button", name="Preferences"))
    )
    obs = adapter.observe(app)
    assert [w.info.title for w in obs.windows] == ["Agentify Fixture vB", "Preferences"]
    adapter.perform(
        app, Action(type="invoke", selector=Selector(role="RadioButton", name="Green", exact=True))
    )
    assert adapter.get_property(app, Selector(automation_id="RadioRed"), "IsSelected") is False
    adapter.perform(app, Action(type="invoke", selector=Selector(automation_id="ApplyButton")))
    adapter.perform(
        app, Action(type="wait_for", gone=True, selector=Selector(automation_id="ApplyButton"))
    )
    assert json.loads((tmp_path / "profile.json").read_text())["colour"] == "Green"


def test_slugs_change_on_relaunch_but_automation_ids_do_not(desktop: FakeDesktop) -> None:
    fake = colour_fixture_app(desktop, version="A")
    adapter = FakeAdapter(desktop)
    app = adapter.attach(fake.app_id)
    before = {e.name: e.slug for e in adapter.observe(app).iter_elements() if e.role == "Text"}
    desktop.relaunch(fake.app_id)
    after = {e.name: e.slug for e in adapter.observe(app).iter_elements() if e.role == "Text"}
    assert before["Notes"] != after["Notes"]
    with pytest.raises(ElementNotFound):
        adapter.perform(app, Action(type="locate", selector=Selector(slug=before["Notes"])))
    loc = adapter.perform(
        app,
        Action(
            type="locate",
            selector=Selector(slug=before["Notes"], role="Text", name="Notes", exact=True),
        ),
    )
    assert loc.resolved_by == "role_name"


def test_errors_and_guards(desktop: FakeDesktop) -> None:
    fake = colour_fixture_app(desktop, version="A")
    adapter = FakeAdapter(desktop)
    app = adapter.attach(fake.app_id)
    with pytest.raises(AmbiguousSelector):
        adapter.perform(app, Action(type="locate", selector=Selector(name="Colour")))
    with pytest.raises(ActionNotSupported):
        adapter.perform(
            app,
            Action(type="invoke", pattern="expand", selector=Selector(automation_id="SaveButton")),
        )
    with pytest.raises(PermissionDenied):
        adapter.perform(app, Action(type="send_keys", keys="alt+f4"))
    with pytest.raises(AdapterTimeout):
        adapter.perform(app, Action(type="wait_for", selector=Selector(name="Nope")))
    desktop.close(fake.app_id)
    assert not adapter.read_state(app).alive
    with pytest.raises(WindowGone):
        adapter.observe(app)


def test_simple_editor_and_screenshot(desktop: FakeDesktop, tmp_path: Path) -> None:
    fake = simple_editor_app(desktop, state_file=tmp_path / "doc.json")
    adapter = FakeAdapter(desktop, screenshots_dir=tmp_path)
    app = adapter.attach(fake.app_id)
    adapter.perform(
        app, Action(type="focus", selector=Selector(role="Document", name="Text editor"))
    )
    adapter.perform(app, Action(type="send_keys", keys="hello world", verbatim=True))
    adapter.perform(app, Action(type="invoke", selector=Selector(automation_id="SaveButton")))
    assert json.loads((tmp_path / "doc.json").read_text()) == {"text": "hello world"}
    shot = adapter.perform(app, Action(type="screenshot")).screenshot_path
    assert shot is not None and shot.exists()


@pytest.mark.parametrize("cls", [BrowserAdapter, MacOSAdapter, LinuxAdapter])
def test_unsupported_adapters_raise(cls: type) -> None:
    with pytest.raises(UnsupportedPlatformError):
        cls().list_applications()


def test_registry(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "none.env", environ={})
    assert isinstance(create_adapter("fake", settings), FakeAdapter)
    assert create_adapter("browser", settings).platform == "browser"
    assert "unsupported" in platform_support()["macos"]
