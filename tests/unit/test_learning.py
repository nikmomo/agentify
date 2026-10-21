"""Exploration trace -> workflow extraction, and deterministic replay (FakeAdapter)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentify.adapters.base import Action, Selector
from agentify.errors import ApprovalRequired, InvalidState, LearnError, LimitExceeded
from agentify.learning import extract_workflow, slugify_skill_id
from agentify.workflows.executor import run_workflow
from agentify.workflows.models import Expectation, SelectorSpec

from .helpers import (
    COLOUR_STEP,
    LABEL,
    NOTES_STEP,
    PROFILE_BINDINGS,
    PROFILE_INPUTS,
    SAVE_STEP,
    Env,
    explore_set_profile,
    make_env,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


def record(env: Env, eid: str) -> dict:
    ex = env.executions.get(eid)
    return {"execution_id": eid, "trace": ex.trace, "verifications": ex.verifications}


def extract(env: Env, eid: str, **over):
    kwargs = dict(
        execution=record(env, eid),
        skill_id="set-profile",
        app_key="powershell",
        name="Set profile",
        description="Sets notes and colour, then saves",
        inputs=PROFILE_INPUTS,
        bindings=PROFILE_BINDINGS,
    )
    kwargs.update(over)
    return extract_workflow(**kwargs)


def test_trace_records_every_action_including_failures(env: Env) -> None:
    eid = explore_set_profile(env)
    trace = env.executions.get(eid).trace
    assert [e["index"] for e in trace] == [0, 1, 2, 3, 4]
    assert trace[0]["ok"] is False and trace[0]["error"]["code"] == "element_not_found"
    assert trace[COLOUR_STEP]["element"] == {
        "role": "ComboBox",
        "name": "Colour",
        "automation_id": "ColourCombo",
        "stable_automation_id": True,
        "value": "Red",
    }
    # Persisted as evidence too.
    assert len(env.store.execution(eid)["trace"]) == 5


def test_extract_builds_parameterized_durable_workflow(env: Env) -> None:
    eid = explore_set_profile(env)
    wf, used = extract(env, eid)
    assert used == {"notes": "hello", "colour": "Blue"}
    assert [s.action for s in wf.steps] == [
        "set_value",
        "set_value",
        "click",
    ]  # failure + read dropped
    s1, s2, s3 = wf.steps
    assert s1.value == "{{notes}}" and s1.selector is not None
    assert s1.selector.automation_id == "NotesBox" and s1.selector.fallback is not None
    assert s1.selector.fallback.name == "Notes" and s1.selector.slug is None
    assert (
        s2.value == "{{colour}}"
        and s2.selector is not None
        and s2.selector.automation_id == "ColourCombo"
    )
    assert (
        s3.idempotent is False
        and s3.selector is not None
        and s3.selector.automation_id == "SaveButton"
    )
    assert wf.verification[0].expected == "Current colour: {{colour}}"
    assert wf.verification[0].property == "name"
    assert wf.status == "candidate" and wf.risk.value == "local_configuration_change"
    assert wf.provenance.source_execution_id == eid


def test_replay_with_new_parameters_is_verified(env: Env) -> None:
    wf, _ = extract(env, explore_set_profile(env))
    app = env.adapter.attach(env.fake.app_id)
    result = run_workflow(env.adapter, app, wf, {"notes": "second", "colour": "Green"})
    assert result.verified and result.failure is None and result.action_count == 3
    assert env.profile() == {"colour": "Green", "notes": "second", "version": "A"}
    assert [s.resolved_by for s in result.steps] == ["automation_id"] * 3


def test_replay_failures_are_categorised(env: Env) -> None:
    wf, _ = extract(env, explore_set_profile(env))
    app = env.adapter.attach(env.fake.app_id)
    bad = run_workflow(env.adapter, app, wf, {"notes": "x", "colour": "Pink"})
    assert not bad.verified and bad.failure is not None and bad.failure.category == "bad_parameters"
    assert bad.action_count == 0, "no UI input on bad parameters"

    capped = run_workflow(env.adapter, app, wf, {"notes": "x", "colour": "Red"}, max_actions=2)
    assert capped.failure is not None and capped.failure.category == "limit_exceeded"

    # Verification that cannot pass -> verification_failed (all steps ran).
    wrong = wf.model_copy(
        update={
            "verification": [
                wf.verification[0].model_copy(update={"expected": "nope", "timeout_ms": 0})
            ]
        }
    )
    res = run_workflow(env.adapter, app, wrong, {"notes": "x", "colour": "Red"})
    assert not res.verified and res.failure is not None
    assert res.failure.category == "verification_failed" and res.action_count == 3


def test_fallback_selector_used_when_primary_missing(env: Env) -> None:
    wf, _ = extract(env, explore_set_profile(env))
    s2 = wf.steps[1]
    assert s2.selector is not None and s2.selector.fallback is not None
    broken = wf.model_copy(
        update={
            "steps": [
                wf.steps[0],
                s2.model_copy(
                    update={"selector": s2.selector.model_copy(update={"automation_id": "Gone"})}
                ),
                wf.steps[2],
            ]
        }
    )
    app = env.adapter.attach(env.fake.app_id)
    result = run_workflow(env.adapter, app, broken, {"notes": "n", "colour": "Red"})
    assert result.verified and result.fallback_used and result.steps[1].used_fallback


def test_unresolvable_step_stops_before_later_steps(env: Env) -> None:
    wf, _ = extract(env, explore_set_profile(env))
    s1 = wf.steps[0]
    assert s1.selector is not None
    broken = wf.model_copy(
        update={
            "steps": [
                s1.model_copy(
                    update={
                        "selector": s1.selector.model_copy(
                            update={"automation_id": "X", "fallback": None}
                        )
                    }
                ),
                *wf.steps[1:],
            ]
        }
    )
    calls_before = len(env.adapter.calls)
    res = run_workflow(
        env.adapter, env.adapter.attach(env.fake.app_id), broken, {"notes": "n", "colour": "Red"}
    )
    assert (
        res.failure is not None
        and res.failure.category == "selector_unresolved"
        and res.failure.step_id == "s1"
    )
    assert len(env.adapter.calls) == calls_before + 1, "no further input after a failed step"


def test_list_item_selection_templates_the_selector_name(env: Env) -> None:
    ex = env.executions.start(env.fake.app_id, "pick colour from the list")
    combo = Selector(automation_id="ColourCombo")
    env.executions.perform(ex.execution_id, Action(type="invoke", pattern="expand", selector=combo))
    env.executions.perform(
        ex.execution_id,
        Action(type="invoke", pattern="select", selector=Selector(role="ListItem", name="Green")),
    )
    env.executions.verify(
        ex.execution_id,
        Expectation(
            type="ui_value_equals",
            selector=SelectorSpec(automation_id="ColourCombo"),
            expected="Green",
        ),
    )
    wf, used = extract_workflow(
        execution=record(env, ex.execution_id),
        skill_id="pick-colour",
        app_key="powershell",
        name="Pick colour",
        description="Choose a colour in the dropdown",
        inputs={"colour": {"example": "Green"}},
        bindings={"colour": [1]},
    )
    sel = wf.steps[1].selector
    assert sel is not None and sel.role == "ListItem" and sel.name == "{{colour}}" and sel.exact
    assert sel.automation_id is None and wf.verification[0].expected == "{{colour}}"
    res = run_workflow(env.adapter, env.adapter.attach(env.fake.app_id), wf, {"colour": "Blue"})
    assert res.verified and used == {"colour": "Green"}


@pytest.mark.parametrize(
    ("over", "fragment"),
    [
        ({"bindings": {"notes": [NOTES_STEP], "colour": [99]}}, "not a kept"),
        ({"bindings": {"notes": [NOTES_STEP], "colour": [0]}}, "not a kept"),  # the failed click
        ({"bindings": {"nope": [COLOUR_STEP]}}, "undeclared"),
        ({"bindings": {"notes": [NOTES_STEP], "colour": [SAVE_STEP]}}, "not found in step"),
        ({"bindings": {"notes": [NOTES_STEP]}}, "not bound to any step"),
        ({"keep_steps": [1]}, "observation-only"),
        ({"keep_steps": [42]}, "unknown trace"),
        ({"inputs": {"colour": {"type": "enum"}}}, "invalid input spec"),
    ],
)
def test_extract_rejects_bad_requests(env: Env, over: dict, fragment: str) -> None:
    eid = explore_set_profile(env)
    with pytest.raises(LearnError) as info:
        extract(env, eid, **over)
    assert fragment in str(info.value)


def test_extract_requires_final_verification(env: Env) -> None:
    ex = env.executions.start(env.fake.app_id, "no check")
    env.executions.verify(
        ex.execution_id,
        Expectation(
            type="ui_value_equals",
            selector=LABEL,
            property="name",
            expected="Current colour: (unsaved)",
        ),
    )
    env.executions.perform(
        ex.execution_id,
        Action(type="set_value", selector=Selector(automation_id="NotesBox"), value="a"),
    )
    with pytest.raises(LearnError, match="no passed verification after the last kept action"):
        extract_workflow(
            execution=record(env, ex.execution_id),
            skill_id="x-skill",
            app_key="powershell",
            name="x",
            description="x",
            inputs={},
            bindings={},
        )


def test_keep_steps_selects_subset(env: Env) -> None:
    eid = explore_set_profile(env)
    wf, _ = extract(
        env,
        eid,
        keep_steps=[COLOUR_STEP, SAVE_STEP],
        inputs={"colour": PROFILE_INPUTS["colour"]},
        bindings={"colour": [COLOUR_STEP]},
    )
    assert [s.action for s in wf.steps] == ["set_value", "click"]


def test_execution_guards(tmp_path: Path) -> None:
    env = make_env(
        tmp_path,
        AGENTIFY_MAX_ACTIONS_PER_EXECUTION=2,
        AGENTIFY_APPROVAL_THRESHOLD="local_configuration_change",
    )
    ex = env.executions.start(env.fake.app_id, "guards")
    with pytest.raises(ApprovalRequired):
        env.executions.perform(
            ex.execution_id,
            Action(type="set_value", selector=Selector(automation_id="NotesBox"), value="a"),
        )
    env.executions.perform(
        ex.execution_id, Action(type="read_value", selector=Selector(automation_id="NotesBox"))
    )
    env.executions.perform(
        ex.execution_id,
        Action(type="set_value", selector=Selector(automation_id="NotesBox"), value="a"),
        approved=True,
    )
    with pytest.raises(LimitExceeded):
        env.executions.perform(
            ex.execution_id, Action(type="read_value", selector=Selector(automation_id="NotesBox"))
        )
    env.executions.close(ex.execution_id, "aborted")
    with pytest.raises(Exception):  # noqa: B017 - NotFound after close
        env.executions.perform(
            ex.execution_id, Action(type="read_value", selector=Selector(automation_id="NotesBox"))
        )
    assert env.store.execution(ex.execution_id)["outcome"] == "aborted"
    with pytest.raises(InvalidState):
        ex.open = False
        env.executions._live[ex.execution_id] = ex
        env.executions.perform(
            ex.execution_id, Action(type="read_value", selector=Selector(automation_id="NotesBox"))
        )


def test_slugify_skill_id() -> None:
    assert slugify_skill_id("Set Profile Colour!") == "set-profile-colour"
    assert slugify_skill_id("123 go") == "skill-123-go"
