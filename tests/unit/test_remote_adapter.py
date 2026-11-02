"""RemoteAdapter <-> AdapterHost over a temp folder, with the FakeAdapter behind the host."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentify.adapters import create_adapter
from agentify.adapters.base import (
    Action,
    AdapterTimeout,
    AdapterUnavailable,
    ElementNotFound,
    PermissionDenied,
    PlatformAdapter,
    Selector,
)
from agentify.adapters.fake import FakeAdapter, FakeDesktop, colour_fixture_app
from agentify.adapters.remote import AdapterHost, RemoteAdapter
from agentify.adapters.remote.wire import RESPONSES, SHOTS, from_shared, to_shared
from agentify.config import load_settings

pytestmark = pytest.mark.unit

COMBO = Selector(automation_id="ColourCombo", role="ComboBox", name="Colour")


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[RemoteAdapter, AdapterHost, str]]:
    rpc = tmp_path / "rpc"
    desktop = FakeDesktop()
    fake = colour_fixture_app(desktop, version="A", state_file=tmp_path / "profile.json")
    host = AdapterHost(
        rpc,
        FakeAdapter(desktop, screenshots_dir=rpc / SHOTS),
        allow_launch=[tmp_path / "apps"],
        poll_s=0.005,
    )
    thread = threading.Thread(target=host.serve_forever, daemon=True)
    thread.start()
    host.beat()
    client = RemoteAdapter(rpc, timeout_ms=5_000, poll_s=0.005)
    try:
        yield client, host, fake.app_id
    finally:
        host.stop()
        thread.join(5)
        client.close()


def test_remote_satisfies_protocol(tmp_path: Path) -> None:
    assert isinstance(RemoteAdapter(tmp_path), PlatformAdapter)


def test_round_trip_matches_the_local_adapter(
    served: tuple[RemoteAdapter, AdapterHost, str],
) -> None:
    client, _, app_id = served
    assert client.ping()["protocol"] == 1
    assert [a.app_id for a in client.list_applications()] == [app_id]
    app = client.attach(app_id)
    assert app.app_id == app_id
    assert client.observe(app, depth=5).windows
    client.perform(app, Action(type="invoke", pattern="expand", selector=COMBO))
    r = client.perform(
        app,
        Action(
            type="invoke",
            pattern="select",
            selector=Selector(role="ListItem", name="Blue", exact=True),
        ),
    )
    assert r.ok and r.resolved_by == "role_name"
    assert client.read_value(app, COMBO) == "Blue"
    assert client.find_elements(app, COMBO)
    assert client.read_state(app).window_title
    assert client.ensure_visible(app) is True


def test_typed_errors_cross_the_wire(served: tuple[RemoteAdapter, AdapterHost, str]) -> None:
    client, _, app_id = served
    app = client.attach(app_id)
    with pytest.raises(ElementNotFound):
        client.perform(app, Action(type="invoke", selector=Selector(role="ListItem", name="Nope")))


def test_screenshots_resolve_on_the_client_side(
    served: tuple[RemoteAdapter, AdapterHost, str],
) -> None:
    client, _, app_id = served
    shot = client.capture_screenshot(client.attach(app_id))
    assert shot.is_file()
    assert shot.parent == client.dir / SHOTS


def test_launch_is_limited_to_allowed_folders(
    served: tuple[RemoteAdapter, AdapterHost, str], tmp_path: Path
) -> None:
    client, _, _ = served
    outside = tmp_path / "elsewhere" / "tool.exe"
    with pytest.raises(PermissionDenied):
        client.launch_app(str(outside.resolve()))
    with pytest.raises(ElementNotFound):
        client.launch_app(str((tmp_path / "apps" / "missing.exe").resolve()))


def test_concurrent_clients_get_their_own_answers(
    served: tuple[RemoteAdapter, AdapterHost, str],
) -> None:
    client, _, app_id = served
    other = RemoteAdapter(client.dir, timeout_ms=5_000, poll_s=0.005)
    results: list[str] = []

    def work(c: RemoteAdapter) -> None:
        for _ in range(40):
            results.append(c.attach(app_id).app_id)

    threads = [
        threading.Thread(target=work, args=(c,)) for c in (client, other, client, other, client)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    other.close()
    assert results == [app_id] * 200


def test_no_host_is_unavailable(tmp_path: Path) -> None:
    with pytest.raises(AdapterUnavailable):
        RemoteAdapter(tmp_path / "nobody").list_applications()


def test_an_unanswered_perform_may_have_happened(tmp_path: Path) -> None:
    rpc = tmp_path / "rpc"
    host = AdapterHost(rpc, FakeAdapter())
    host.beat()  # a heartbeat, but nothing serving requests
    client = RemoteAdapter(rpc, timeout_ms=100, poll_s=0.005, action_grace_ms=0)
    app_json = {
        "app_id": "x",
        "platform": "fake",
        "hwnd": 1,
        "pid": 1,
        "process_name": "x",
        "window_title": "x",
    }
    from agentify.adapters.base.models import AppHandle

    with pytest.raises(AdapterTimeout) as info:
        client.perform(
            AppHandle.model_validate(app_json),
            Action(type="invoke", selector=COMBO),
            timeout_ms=1,
        )
    assert info.value.details["possibly_performed"] is True
    client.close()
    assert (rpc / RESPONSES).read_text(encoding="utf-8") == ""


def test_paths_travel_relative_to_the_shared_folder(tmp_path: Path) -> None:
    rpc = tmp_path / "rpc"
    inside = rpc / SHOTS / "a.png"
    assert to_shared(inside, rpc) == "shots/a.png"
    assert from_shared("shots/a.png", tmp_path / "view") == tmp_path / "view" / "shots" / "a.png"
    outside = tmp_path / "x.png"
    assert to_shared(outside, rpc) == str(outside)


def test_registry_builds_a_remote_adapter(tmp_path: Path) -> None:
    settings = load_settings(
        tmp_path / "none.env",
        environ={"AGENTIFY_ADAPTER": "remote", "AGENTIFY_REMOTE_DIR": str(tmp_path / "rpc")},
    )
    adapter = create_adapter(settings=settings)
    assert isinstance(adapter, RemoteAdapter)
    adapter.close()
    with pytest.raises(AdapterUnavailable):
        create_adapter(
            settings=load_settings(tmp_path / "none.env", environ={"AGENTIFY_ADAPTER": "remote"})
        )


class _RemoteFake(FakeAdapter):
    """A FakeAdapter that looks remote: open_app must launch through it, not locally."""

    remote = True

    def __init__(self, desktop: FakeDesktop, state_file: Path) -> None:
        super().__init__(desktop)
        self.state_file = state_file
        self.launched: list[str] = []

    def launch_app(self, app: str) -> dict[str, str]:
        self.launched.append(app)
        colour_fixture_app(self.desktop, version="A", state_file=self.state_file)
        return {"launched": app, "via": "shortcut"}


def test_open_app_launches_on_the_remote_side(tmp_path: Path) -> None:
    from agentify.mcp import build_app
    from agentify.storage import Store

    from .helpers import settings

    adapter = _RemoteFake(FakeDesktop(), tmp_path / "profile.json")
    s = settings(tmp_path)
    app = build_app(adapter, s, Store(s.db_path, s.skills_dir))
    opened = app.open_app("powershell", timeout_s=5)
    assert adapter.launched == ["powershell"]
    assert opened["launched"] == "powershell"
    assert not (s.data_dir / "apps.json").exists(), "a remote pid says nothing about local files"
