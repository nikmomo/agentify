"""discover / learn / update through the real MCP server on a real Windows desktop.

Real WinApp CLI adapter + in-process MCP client, against our own AgentifyFixture.exe (vA).
This is the evidence that learning works on real UI Automation, not just the FakeAdapter.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from agentify.config import Settings
from agentify.mcp import build_app, tool_name_for
from agentify.storage import Store

from .conftest import RunningFixture

pytestmark = [pytest.mark.integration, pytest.mark.windows]

APP_KEY = "agentifyfixture"
TOOL = tool_name_for(APP_KEY, "set-profile")


def make_settings(tmp: Path) -> Settings:
    return Settings.model_validate(
        {
            "data_dir": tmp,
            "db_path": tmp / "agentify.sqlite",
            "skills_dir": tmp / "skills",
            "screenshots_dir": tmp / "shots",
        }
    )


def payload(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    if result.structured_content:
        data = result.structured_content
        return data.get("result", data) if set(data) == {"result"} else data
    return json.loads(result.content[0].text)


async def call(c: Client, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    return payload(await c.call_tool(tool, args or {}))


async def explore(c: Client, app_id: str, notes: str, colour: str) -> tuple[str, list[int]]:
    eid = (
        await call(
            c, "start_learning", {"app_id": app_id, "goal": f"set notes and colour to {colour}"}
        )
    )["execution_id"]
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


async def test_discover_learn_update_on_real_fixture(
    fixture_exe_a: RunningFixture, tmp_path: Path
) -> None:
    fx = fixture_exe_a
    settings = make_settings(tmp_path / "data")
    app = build_app(fx.adapter, settings)
    async with Client(app.server) as c:
        # ---- discover
        apps = (await call(c, "list_apps"))["apps"]
        mine = [a for a in apps if a["app_id"] == fx.app.app_id]
        assert mine and mine[0]["app_key"] == APP_KEY
        outline = (await call(c, "inspect_app", {"app_id": fx.app.app_id}))["outline"]
        assert (
            "ComboBox 'Colour' #ColourCombo" in outline and "Button 'Save' #SaveButton" in outline
        )

        # ---- learn
        eid, steps = await explore(c, fx.app.app_id, "learned on real UIA", "Blue")
        assert fx.profile() == {"colour": "Blue", "notes": "learned on real UIA", "version": "A"}
        learned = await call(
            c,
            "learn_skill",
            {
                "execution_id": eid,
                "name": "Set profile",
                "description": "Sets the profile notes and colour in Agentify Fixture, then saves",
                "skill_id": "set-profile",
                "inputs": {
                    "notes": {"type": "string", "example": "learned on real UIA"},
                    "colour": {"type": "enum", "enum": ["Red", "Green", "Blue"], "example": "Blue"},
                },
                "bindings": {"notes": [steps[0]], "colour": [steps[1]]},
            },
        )
        assert learned["tool_name"] == TOOL and learned["status"] == "candidate"
        assert TOOL in {t.name for t in (await c.list_tools()).tools}
        yaml_text = (await call(c, "get_skill", {"skill_id": "set-profile"}))["yaml"]
        assert "automation_id: NotesBox" in yaml_text and "slug" not in yaml_text

        # ---- use the generated tool (auto-finds the window by app_key), promote on distinct params
        t0 = time.perf_counter()
        r = await call(c, TOOL, {"notes": "replayed", "colour": "Green"})
        wall_ms = (time.perf_counter() - t0) * 1000
        assert r["verified"] and r["promoted_to_active"] and r["llm_calls"] == 0, r
        assert fx.profile() == {"colour": "Green", "notes": "replayed", "version": "A"}
        print(
            f"\nreal replay via generated tool: {r['duration_ms']} ms runtime, {wall_ms:.0f} ms wall"
        )

        # ---- update: metadata
        up = await call(
            c,
            "update_skill",
            {"skill_id": "set-profile", "description": "Updates notes + colour and saves"},
        )
        assert up["new_version"] == 2 and up["status"] == "active"
        tool = {t.name: t for t in (await c.list_tools()).tools}[TOOL]
        assert "Updates notes + colour" in (tool.description or "")

        # ---- update: re-learn a colour-only flow from a new exploration
        eid2, steps2 = await explore(c, fx.app.app_id, "unchanged", "Red")
        up2 = await call(
            c,
            "update_skill",
            {
                "skill_id": "set-profile",
                "execution_id": eid2,
                "description": "Sets only the colour and saves",
                "inputs": {
                    "colour": {"type": "enum", "enum": ["Red", "Green", "Blue"], "example": "Red"}
                },
                "bindings": {"colour": [steps2[1]]},
                "keep_steps": steps2[1:],
            },
        )
        assert up2["new_version"] == 3 and up2["serving_version"] == 2
        r3 = await call(
            c, "run_skill", {"skill_id": "set-profile", "params": {"colour": "Blue"}, "version": 3}
        )
        assert r3["verified"] and r3["promoted_to_active"]
        tool = {t.name: t for t in (await c.list_tools()).tools}[TOOL]
        assert set(tool.input_schema["required"]) == {"colour"}
        assert fx.profile()["colour"] == "Blue" and fx.profile()["notes"] == "unchanged"

    # ---- restart: tools come back from storage and still work on the live app
    restarted = build_app(fx.adapter, settings, Store(settings.db_path, settings.skills_dir))
    async with Client(restarted.server) as c:
        r = await call(c, TOOL, {"colour": "Green"})
        assert r["verified"] and fx.profile()["colour"] == "Green"
