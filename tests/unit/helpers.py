"""Shared builders for runtime/learning/skills unit tests (FakeAdapter only: logic, not Windows)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentify.adapters.base import Action, Selector
from agentify.adapters.fake import FakeAdapter, FakeDesktop
from agentify.adapters.fake.adapter import FakeApp
from agentify.adapters.fake.scenarios import colour_fixture_app
from agentify.config import Settings
from agentify.runtime import ExecutionManager
from agentify.skills import SkillService
from agentify.storage import Store
from agentify.workflows.models import Expectation, SelectorSpec

NOTES = Selector(role="Edit", name="Notes")
COMBO = Selector(role="ComboBox", name="Colour")
SAVE = Selector(role="Button", name="Save", exact=True)
LABEL = SelectorSpec(automation_id="StatusLabel")


def settings(tmp: Path, **over: Any) -> Settings:
    base: dict[str, Any] = {
        "data_dir": tmp,
        "db_path": tmp / "agentify.sqlite",
        "skills_dir": tmp / "skills",
        "screenshots_dir": tmp / "shots",
    }
    return Settings.model_validate(base | over)


@dataclass
class Env:
    desktop: FakeDesktop
    fake: FakeApp
    adapter: FakeAdapter
    store: Store
    executions: ExecutionManager
    skills: SkillService
    state_file: Path

    def profile(self) -> dict[str, Any]:
        import json

        return json.loads(self.state_file.read_text(encoding="utf-8"))


def make_env(tmp: Path, **settings_over: Any) -> Env:
    desktop = FakeDesktop()
    state = tmp / "profile.json"
    fake = colour_fixture_app(desktop, version="A", state_file=state)
    adapter = FakeAdapter(desktop)
    s = settings(tmp, **settings_over)
    store = Store(s.db_path, s.skills_dir)
    executions = ExecutionManager(adapter, store, s)
    return Env(
        desktop,
        fake,
        adapter,
        store,
        executions,
        SkillService(adapter, store, s, executions),
        state,
    )


def explore_set_profile(
    env: Env, notes: str = "hello", colour: str = "Blue", *, noise: bool = True
) -> str:
    """Host-like exploration: a failed attempt, a read, the real actions, and a final verification."""
    ex = env.executions.start(env.fake.app_id, "set profile notes and colour")
    eid = ex.execution_id
    if noise:
        env.executions.perform(
            eid, Action(type="click", selector=Selector(role="Button", name="Apply"))
        )  # fails
        env.executions.perform(eid, Action(type="read_value", selector=COMBO))  # observation only
    env.executions.perform(eid, Action(type="set_value", selector=NOTES, value=notes))
    env.executions.perform(eid, Action(type="set_value", selector=COMBO, value=colour))
    env.executions.perform(eid, Action(type="click", selector=SAVE, idempotent=False))
    env.executions.verify(
        eid,
        Expectation(
            type="ui_value_equals",
            selector=LABEL,
            property="name",
            expected=f"Current colour: {colour}",
        ),
    )
    return eid


# Trace indexes of the meaningful steps produced by explore_set_profile(noise=True).
NOTES_STEP, COLOUR_STEP, SAVE_STEP = 2, 3, 4
PROFILE_INPUTS = {
    "notes": {"type": "string", "description": "Profile notes", "example": "hello"},
    "colour": {"type": "enum", "enum": ["Red", "Green", "Blue"], "example": "Blue"},
}
PROFILE_BINDINGS = {"notes": [NOTES_STEP], "colour": [COLOUR_STEP]}
