"""MCP server end to end, in-process (real MCP client/server, FakeAdapter desktop).

Covers discover (list_apps, inspect_app), learn (start_learning -> perform_action ->
verify_outcome -> learn_skill -> generated tool + tools/list_changed), and update (metadata and
re-learn), plus restart re-registration and structured errors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from agentify.adapters.fake import FakeAdapter, FakeDesktop
from agentify.adapters.fake.scenarios import colour_fixture_app
from agentify.mcp import AgentifyApp, build_app, tool_name_for
from agentify.storage import Store

from .helpers import settings

pytestmark = pytest.mark.unit
TOOL = tool_name_for("powershell", "set-profile")


def payload(result: Any) -> dict[str, Any]:
    if result.structured_content:
        data = result.structured_content
        return data.get("result", data) if set(data) == {"result"} else data
    return json.loads(result.content[0].text)


def error_of(result: Any) -> dict[str, Any]:
    assert result.is_error, result
    text = result.content[0].text  # SDK prefix: "Error executing tool <name>: {json}"
    return json.loads(text[text.index("{") :])["error"]


class Harness:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.desktop = FakeDesktop()
        self.state = tmp / "profile.json"
        self.fake = colour_fixture_app(self.desktop, version="A", state_file=self.state)
        self.adapter = FakeAdapter(self.desktop)
        self.settings = settings(tmp)
        self.store = Store(self.settings.db_path, self.settings.skills_dir)
        self.app: AgentifyApp = build_app(self.adapter, self.settings, self.store)

    def profile(self) -> dict[str, Any]:
        return json.loads(self.state.read_text(encoding="utf-8"))


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


async def call(c: Client, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    r = await c.call_tool(tool, args or {})
    assert not r.is_error, r.content
    return payload(r)


async def tool_names(c: Client) -> set[str]:
    return {t.name for t in (await c.list_tools()).tools}


async def explore(c: Client, app_id: str, notes: str, colour: str) -> tuple[str, list[int]]:
    ex = await call(
        c, "start_learning", {"app_id": app_id, "goal": f"set notes and colour {colour}"}
    )
    eid = ex["execution_id"]
    steps = []
    for action in (
        {"type": "set_value", "selector": {"role": "Edit", "name": "Notes"}, "value": notes},
        {"type": "set_value", "selector": {"role": "ComboBox", "name": "Colour"}, "value": colour},
        {"type": "click", "selector": {"role": "Button", "name": "Save", "exact": True}},
    ):
        r = await call(c, "perform_action", {"execution_id": eid, "action": action})
        assert r["ok"], r
        steps.append(r["index"])
    v = await call(
        c,
        "verify_outcome",
        {
            "execution_id": eid,
            "expectation": {
                "type": "ui_value_equals",
                "selector": {"automation_id": "StatusLabel"},
                "property": "name",
                "expected": f"Current colour: {colour}",
            },
        },
    )
    assert v["passed"], v
    return eid, steps


INPUTS = {
    "notes": {"type": "string", "description": "Profile notes", "example": "hello"},
    "colour": {"type": "enum", "enum": ["Red", "Green", "Blue"], "example": "Blue"},
}


async def learn_profile(c: Client, h: Harness) -> dict[str, Any]:
    eid, steps = await explore(c, h.fake.app_id, "hello", "Blue")
    return await call(
        c,
        "learn_skill",
        {
            "execution_id": eid,
            "name": "Set profile",
            "description": "Sets the profile notes and colour, then saves",
            "skill_id": "set-profile",
            "inputs": INPUTS,
            "bindings": {"notes": [steps[0]], "colour": [steps[1]]},
        },
    )


async def test_discover_tools(h: Harness) -> None:
    async with Client(h.app.server) as client:
        apps = (await call(client, "list_apps"))["apps"]
        assert apps == [
            {
                "app_id": h.fake.app_id,
                "app_key": "powershell",
                "title": "Agentify Fixture vA",
                "foreground": False,
                "learned_skills": 0,
            }
        ]
        outline = await call(client, "inspect_app", {"app_id": h.fake.app_id})
        text = outline["outline"]
        assert "ComboBox 'Colour' #ColourCombo value='Red'" in text
        assert "Button 'Save' #SaveButton" in text
        assert "#788468" not in text, "numeric (per-launch) automation ids are hidden"
        small = await call(client, "inspect_app", {"app_id": h.fake.app_id, "max_elements": 2})
        assert small["elements_shown"] <= 2 < small["elements_total"]


async def test_learn_generates_tool_and_announces_it(h: Harness) -> None:
    async with Client(h.app.server) as client:
        assert TOOL not in await tool_names(client)
        events: list[str] = []
        async with client.listen(tools_list_changed=True) as sub:
            learned = await learn_profile(client, h)
            with anyio.move_on_after(2):
                async for ev in sub:
                    events.append(type(ev).__name__)
                    break
        assert events == ["ToolsListChanged"]
        assert learned["tool_name"] == TOOL and learned["status"] == "candidate"
        tools = {t.name: t for t in (await client.list_tools()).tools}
        tool = tools[TOOL]
        assert "v1 candidate" in (tool.description or "")
        schema = tool.input_schema
        assert set(schema["required"]) == {"notes", "colour"}
        assert schema["properties"]["colour"]["enum"] == ["Red", "Green", "Blue"]
        assert "app_id" in schema["properties"] and "ctx" not in schema["properties"]

        # Use the generated tool: first distinct run promotes the skill and re-announces the tool.
        events.clear()
        async with client.listen(tools_list_changed=True) as sub:
            r = await call(client, TOOL, {"notes": "from tool", "colour": "Green"})
            with anyio.move_on_after(2):
                async for ev in sub:
                    events.append(type(ev).__name__)
                    break
        assert r["verified"] and r["promoted_to_active"] and r["llm_calls"] == 0
        assert events == ["ToolsListChanged"]
        assert h.profile() == {"colour": "Green", "notes": "from tool", "version": "A"}
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "v1 active" in (tools[TOOL].description or "")

        listed = (await call(client, "list_skills"))["skills"]
        assert listed[0]["tool_name"] == TOOL and listed[0]["status"] == "active"
        assert (await call(client, "list_apps"))["apps"][0]["learned_skills"] == 1
        yaml_text = (await call(client, "get_skill", {"skill_id": "set-profile"}))["yaml"]
        assert "{{colour}}" in yaml_text and "slug" not in yaml_text


async def test_update_metadata_refreshes_generated_tool(h: Harness) -> None:
    async with Client(h.app.server) as client:
        await learn_profile(client, h)
        events: list[str] = []
        async with client.listen(tools_list_changed=True) as sub:
            out = await call(
                client,
                "update_skill",
                {
                    "skill_id": "set-profile",
                    "description": "Updates the saved profile (notes + colour)",
                },
            )
            with anyio.move_on_after(2):
                async for ev in sub:
                    events.append(type(ev).__name__)
                    break
        assert out["new_version"] == 2 and events == ["ToolsListChanged"]
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert "Updates the saved profile" in (tools[TOOL].description or "")
        assert "v2" in (tools[TOOL].description or "")


async def test_update_from_reexploration_replaces_steps_and_schema(h: Harness) -> None:
    async with Client(h.app.server) as client:
        await learn_profile(client, h)
        await call(client, TOOL, {"notes": "a", "colour": "Red"})  # v1 active
        eid, steps = await explore(client, h.fake.app_id, "ignored", "Green")
        out = await call(
            client,
            "update_skill",
            {
                "skill_id": "set-profile",
                "execution_id": eid,
                "description": "Sets only the colour and saves",
                "inputs": {"colour": INPUTS["colour"] | {"example": "Green"}},
                "bindings": {"colour": [steps[1]]},
                "keep_steps": steps[1:],
            },
        )
        assert out["new_version"] == 2 and out["serving_version"] == 1
        r = await call(
            client,
            "run_skill",
            {"skill_id": "set-profile", "params": {"colour": "Blue"}, "version": 2},
        )
        assert r["verified"] and r["promoted_to_active"]
        tool = {t.name: t for t in (await client.list_tools()).tools}[TOOL]
        assert (
            set(tool.input_schema["required"]) == {"colour"}
            and "notes" not in tool.input_schema["properties"]
        )
        assert (await call(client, TOOL, {"colour": "Red"}))["verified"]


async def test_errors_are_structured(h: Harness) -> None:
    async with Client(h.app.server) as client:
        err = error_of(
            await client.call_tool(
                "perform_action",
                {
                    "execution_id": "exec_missing",
                    "action": {"type": "locate", "selector": {"name": "x"}},
                },
            )
        )
        assert err["code"] == "not_found"
        ex = await call(client, "start_learning", {"app_id": h.fake.app_id, "goal": "g"})
        err = error_of(
            await client.call_tool(
                "perform_action", {"execution_id": ex["execution_id"], "action": {"type": "click"}}
            )
        )
        assert err["code"] == "bad_parameters"
        err = error_of(
            await client.call_tool(
                "learn_skill", {"execution_id": ex["execution_id"], "name": "x", "description": "y"}
            )
        )
        assert err["code"] == "learn_error"
        failed = await call(
            client,
            "perform_action",
            {
                "execution_id": ex["execution_id"],
                "action": {"type": "click", "selector": {"name": "Nope"}},
            },
        )
        assert failed["ok"] is False and failed["error"]["code"] == "element_not_found"
        closed = await call(client, "abandon_learning", {"execution_id": ex["execution_id"]})
        assert closed["outcome"] == "aborted"
        rec = await call(client, "get_execution", {"execution_id": ex["execution_id"]})
        assert len(rec["trace"]) == 1 and rec["status"] == "closed"
        await learn_profile(client, h)
        # The generated tool's enum is enforced by the MCP argument schema before any UI input.
        bad = await client.call_tool(TOOL, {"notes": "x", "colour": "Pink"})
        assert bad.is_error and "Pink" in bad.content[0].text


async def test_generated_tools_survive_restart(h: Harness) -> None:
    async with Client(h.app.server) as c:
        await learn_profile(c, h)
    restarted = build_app(h.adapter, h.settings, Store(h.settings.db_path, h.settings.skills_dir))
    async with Client(restarted.server) as c:
        assert TOOL in await tool_names(c)
        assert (await call(c, TOOL, {"notes": "restart", "colour": "Blue"}))["verified"]


async def test_update_skill_replaces_verification(h: Harness) -> None:
    async with Client(h.app.server) as client:
        learned = await learn_profile(client, h)
        assert learned["warnings"] == []
        out = await call(
            client,
            "update_skill",
            {
                "skill_id": "set-profile",
                "verification": [
                    {
                        "type": "ui_value_equals",
                        "selector": {"automation_id": "NotesBox"},
                        "expected": "{{notes}}",
                    }
                ],
            },
        )
        assert out["new_version"] == 2
        yaml_text = (await call(client, "get_skill", {"skill_id": "set-profile"}))["yaml"]
        assert "expected: '{{notes}}'" in yaml_text and "Current colour" not in yaml_text
        assert (await call(client, TOOL, {"notes": "checked", "colour": "Red"}))["verified"]


# ------------------------------------------------------------------ per-app service


async def test_app_server_exposes_only_plain_named_skill_tools(h: Harness) -> None:
    async with Client(h.app.server) as c:
        await learn_profile(c, h)
    app_srv = build_app(h.adapter, h.settings, h.store, app_key="powershell")
    async with Client(app_srv.server) as c:
        tools = {t.name: t for t in (await c.list_tools()).tools}
        assert set(tools) == {"open_app", "set_profile", "set_profile_batch"}, (
            "the learned endpoints (plus open_app); no learning tools, no namespace"
        )
        assert "for the 'powershell' desktop application" in (c.instructions or "")
        assert (tools["set_profile"].description or "").startswith("[v1 candidate]")
        r = await call(c, "set_profile", {"notes": "via app server", "colour": "Green"})
        assert r["verified"] and r["promoted_to_active"]
    assert h.profile()["notes"] == "via app server"


async def test_app_server_picks_up_skills_learned_elsewhere(h: Harness) -> None:
    app_srv = build_app(h.adapter, h.settings, h.store, app_key="powershell")
    async with Client(app_srv.server) as app_client:
        assert await tool_names(app_client) == {"open_app"}
        async with Client(
            h.app.server
        ) as learn_client:  # another server learns into the same store
            await learn_profile(learn_client, h)
        assert await tool_names(app_client) == {"open_app", "set_profile", "set_profile_batch"}
    other = build_app(h.adapter, h.settings, h.store, app_key="notepad")
    async with Client(other.server) as c:
        assert await tool_names(c) == {"open_app"}, "skills of other apps are not exposed"


async def test_batch_tool_runs_items_in_one_call(h: Harness) -> None:
    async with Client(h.app.server) as c:
        learned = await learn_profile(c, h)
        assert learned["batch_tool_name"] == TOOL + "_batch"
        items = [
            {"notes": f"row {i}", "colour": col} for i, col in enumerate(["Red", "Green", "Blue"])
        ]
        r = await call(c, TOOL + "_batch", {"items": items})
        assert (
            r["all_verified"] and r["attempted"] == 3 and r["verified"] == 3 and r["llm_calls"] == 0
        )
        assert h.profile() == {"colour": "Blue", "notes": "row 2", "version": "A"}
        schema = {t.name: t for t in (await c.list_tools()).tools}[TOOL + "_batch"].input_schema
        assert schema["required"] == ["items"]
        # Enum constraints are enforced per item before any UI input.
        bad = await c.call_tool(TOOL + "_batch", {"items": [{"notes": "x", "colour": "Pink"}]})
        assert bad.is_error


async def test_batch_stops_at_first_failure(h: Harness) -> None:
    from agentify.skills import AppNotRunning

    async with Client(h.app.server) as c:
        await learn_profile(c, h)
        real_run = h.app.skills.run
        calls = 0

        def flaky(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise AppNotRunning("window closed")
            return real_run(*args, **kwargs)

        h.app.skills.run = flaky  # type: ignore[method-assign]
        items = [{"notes": f"r{i}", "colour": "Red"} for i in range(4)]
        r = await call(c, TOOL + "_batch", {"items": items})
        assert r["attempted"] == 2 and r["verified"] == 1 and not r["all_verified"]
        assert r["results"][1]["failure"]["category"] == "app_not_running"
        calls = 0
        r = await call(c, TOOL + "_batch", {"items": items, "stop_on_failure": False})
        assert r["attempted"] == 4 and r["verified"] == 3


async def test_list_capabilities_finds_skills_of_an_app_that_is_not_running(h: Harness) -> None:
    async with Client(h.app.server) as c:
        await learn_profile(c, h)
        h.desktop.close(h.fake.app_id)
        for name in ("PowerShell", "power"):
            caps = (await call(c, "list_capabilities", {"app": name}))["capabilities"]
            assert [x["capability_id"] for x in caps] == ["set-profile"], name
        assert (await call(c, "list_capabilities", {"app": "notepad"}))["capabilities"] == []
