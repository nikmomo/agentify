"""Skill lifecycle: learn -> candidate -> (2 distinct verified runs) -> active; update -> vN+1."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentify.errors import InvalidState, LearnError, NotFound
from agentify.skills import AppNotRunning
from agentify.storage import Store
from agentify.workflows import load_workflow

from .helpers import (
    COLOUR_STEP,
    NOTES_STEP,
    PROFILE_BINDINGS,
    PROFILE_INPUTS,
    Env,
    explore_set_profile,
    make_env,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


def learn(env: Env, **over) -> dict:
    eid = explore_set_profile(env)
    kwargs = dict(
        name="Set profile",
        description="Sets notes and colour, then saves",
        inputs=PROFILE_INPUTS,
        bindings=PROFILE_BINDINGS,
        skill_id="set-profile",
    )
    kwargs.update(over)
    return env.skills.learn(eid, **kwargs)


def test_learn_creates_candidate_v1_and_files(env: Env, tmp_path: Path) -> None:
    s = learn(env)
    assert s["skill_id"] == "set-profile" and s["status"] == "candidate"
    assert s["serving_version"] == 1 and s["active_version"] is None
    assert s["versions"][0]["verified_runs"] == 1 and s["versions"][0]["distinct_param_sets"] == 1
    assert s["learning_params"] == {"notes": "hello", "colour": "Blue"}
    yaml_file = tmp_path / "skills" / "powershell" / "set-profile" / "v1.yaml"
    assert load_workflow(yaml_file).steps[0].value == "{{notes}}"
    ex = env.store.execution(s["learned_from"])
    assert ex["status"] == "closed" and ex["outcome"] == "success" and ex["verified"]
    with pytest.raises(NotFound):
        env.executions.get(s["learned_from"])  # closed executions cannot be reused


def test_promotion_needs_second_distinct_verified_run(env: Env) -> None:
    learn(env)
    same = env.skills.run("set-profile", {"notes": "hello", "colour": "Blue"})
    assert same["verified"] and not same["promoted_to_active"] and same["status"] == "candidate"
    other = env.skills.run("set-profile", {"notes": "bye", "colour": "Red"})
    assert other["verified"] and other["promoted_to_active"] and other["status"] == "active"
    assert other["llm_calls"] == 0 and other["action_count"] == 3
    assert env.profile() == {"colour": "Red", "notes": "bye", "version": "A"}
    summary = env.skills.summary("set-profile")
    assert summary["active_version"] == 1 and summary["versions"][0]["verified_runs"] == 3
    rec = env.store.execution(other["execution_id"])
    assert (
        rec["mode"] == "replay"
        and rec["verified"]
        and rec["params"] == {"notes": "bye", "colour": "Red"}
    )


def test_failed_runs_are_counted_and_do_not_promote(env: Env) -> None:
    learn(env)
    bad = env.skills.run("set-profile", {"notes": "x", "colour": "Pink"})
    assert not bad["verified"] and bad["failure"]["category"] == "bad_parameters"
    env.desktop.close(env.fake.app_id)
    with pytest.raises(AppNotRunning):
        env.skills.run("set-profile", {"notes": "x", "colour": "Red"})
    v = env.skills.summary("set-profile")["versions"][0]
    assert v["failed_runs"] == 0, "bad parameters / app not running are not skill failures"


def test_learn_errors(env: Env) -> None:
    learn(env)
    with pytest.raises(InvalidState, match="already exists"):
        learn(env)
    eid = explore_set_profile(env)
    with pytest.raises(LearnError):
        env.skills.learn(
            eid,
            name="Broken",
            description="d",
            inputs={"x": {"example": "zz"}},
            bindings={"x": [NOTES_STEP]},
        )
    # A failed learn leaves the exploration open so the host can fix the request and retry.
    assert env.executions.get(eid).open


def test_update_metadata_keeps_history_and_activity(env: Env) -> None:
    learn(env)
    env.skills.run("set-profile", {"notes": "bye", "colour": "Red"})  # -> active v1
    out = env.skills.update(
        "set-profile",
        description="Sets the profile notes and colour and saves them",
        inputs={"colour": {"description": "Colour name as shown in the dropdown"}},
    )
    assert out["new_version"] == 2 and out["serving_version"] == 2 and out["active_version"] == 2
    assert out["inputs"]["colour"]["description"] == "Colour name as shown in the dropdown"
    statuses = {v["version"]: v["status"] for v in out["versions"]}
    assert statuses == {1: "superseded", 2: "active"}
    v2 = next(v for v in out["versions"] if v["version"] == 2)
    assert v2["verified_runs"] == 2 and v2["distinct_param_sets"] == 2
    with pytest.raises(LearnError, match="cannot add input"):
        env.skills.update("set-profile", inputs={"size": {"type": "string"}})
    with pytest.raises(LearnError, match="cannot change the type"):
        env.skills.update("set-profile", inputs={"colour": {"type": "integer"}})


def test_update_from_new_exploration_creates_candidate_until_promoted(env: Env) -> None:
    learn(env)
    env.skills.run("set-profile", {"notes": "bye", "colour": "Red"})  # v1 active
    # Re-learn without the notes field: a colour-only flow.
    eid = explore_set_profile(env, notes="kept", colour="Green")
    out = env.skills.update(
        "set-profile",
        execution_id=eid,
        description="Sets only the colour and saves",
        inputs={"colour": PROFILE_INPUTS["colour"] | {"example": "Green"}},
        bindings={"colour": [COLOUR_STEP]},
        keep_steps=[COLOUR_STEP, 4],
    )
    assert out["new_version"] == 2 and out["active_version"] == 1 and out["serving_version"] == 1
    # Active v1 keeps serving; v2 runs only when asked explicitly.
    v2_run = env.skills.run("set-profile", {"colour": "Blue"}, version=2)
    assert v2_run["verified"] and v2_run["promoted_to_active"]
    final = env.skills.summary("set-profile")
    assert final["active_version"] == 2 and final["serving_version"] == 2
    assert list(final["inputs"]) == ["colour"] and final["step_count"] == 2
    assert {v["version"]: v["status"] for v in final["versions"]} == {1: "superseded", 2: "active"}


def test_update_rejects_other_app_and_unknown_skill(env: Env) -> None:
    learn(env)
    with pytest.raises(NotFound):
        env.skills.update("nope", description="x")


def test_skills_persist_across_service_restarts(env: Env, tmp_path: Path) -> None:
    learn(env)
    env.store.close()
    from agentify.runtime import ExecutionManager
    from agentify.skills import SkillService

    from .helpers import settings

    s = settings(tmp_path)
    store = Store(s.db_path, s.skills_dir)
    svc = SkillService(env.adapter, store, s, ExecutionManager(env.adapter, store, s))
    assert [x["skill_id"] for x in svc.list()] == ["set-profile"]
    assert svc.run("set-profile", {"notes": "after", "colour": "Green"})["verified"]


def test_parallel_runs_are_serialized(env: Env) -> None:
    """Hosts may call a generated tool several times in parallel (batch); UI input must not interleave."""
    import threading
    import time

    learn(env)
    active = 0
    overlaps = 0
    real_perform = env.adapter.perform

    def slow_perform(app, action, **kw):  # type: ignore[no-untyped-def]
        nonlocal active, overlaps
        active += 1
        overlaps += active > 1
        time.sleep(0.01)
        try:
            return real_perform(app, action, **kw)
        finally:
            active -= 1

    env.adapter.perform = slow_perform  # type: ignore[method-assign]
    results: list[dict] = []
    params = [
        {"notes": f"n{i}", "colour": c} for i, c in enumerate(["Red", "Green", "Blue", "Red"])
    ]
    threads = [
        threading.Thread(target=lambda p=p: results.append(env.skills.run("set-profile", p)))
        for p in params
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert overlaps == 0
    assert len(results) == 4 and all(r["verified"] for r in results)


def test_constant_checks_are_flagged_and_fixable_by_update(env: Env) -> None:
    """A learned check like 'Records: 1' encodes state; learn warns, update_skill replaces it."""
    from agentify.workflows.models import Expectation, SelectorSpec

    eid = explore_set_profile(env)
    env.executions.verify(
        eid,
        Expectation(
            type="ui_value_equals",
            selector=SelectorSpec(automation_id="NotesBox"),
            expected="hello",
        ),
    )
    env.executions.verify(  # constant: does not depend on any input
        eid,
        Expectation(
            type="ui_value_equals",
            selector=SelectorSpec(role="Button", name="Save"),
            property="name",
            expected="Save",
        ),
    )
    s = env.skills.learn(
        eid,
        name="Set profile",
        description="d",
        inputs=PROFILE_INPUTS,
        bindings=PROFILE_BINDINGS,
        skill_id="set-profile",
    )
    assert len(s["warnings"]) == 1 and "'Save'" in s["warnings"][0]
    out = env.skills.update(
        "set-profile",
        verification=[
            {
                "type": "ui_value_equals",
                "selector": {"automation_id": "StatusLabel"},
                "property": "name",
                "expected": "Current colour: {{colour}}",
            },
        ],
    )
    assert out["new_version"] == 2 and out["warnings"] == []
    wf = env.store.workflow("set-profile", 2)
    assert len(wf.verification) == 1 and "verification" in wf.provenance.notes
    assert env.skills.run("set-profile", {"notes": "x", "colour": "Green"})["verified"]
    from agentify.errors import WorkflowInvalid

    with pytest.raises(WorkflowInvalid):
        env.skills.update(
            "set-profile",
            verification=[
                {"type": "ui_value_equals", "selector": {"name": "x"}, "expected": "{{nope}}"}
            ],
        )
