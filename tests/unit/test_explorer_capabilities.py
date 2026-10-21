"""explore_app orchestration, capability tools, value_map, preconditions, positional selectors.

The Explorer sub-agent (claude -p) is replaced by an in-process stand-in that drives the same
runtime the real sub-agent's MCP server uses; the real sub-agent runs only against a real desktop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp import Client

import agentify.explorer as explorer_pkg
from agentify.adapters.base import Action, Selector
from agentify.errors import BadParameters, InvalidState
from agentify.explorer import ExplorerReport
from agentify.learning import extract_workflow
from agentify.mcp import build_app
from agentify.workflows.executor import run_workflow
from agentify.workflows.models import Expectation, SelectorSpec

from .helpers import COLOUR_STEP, NOTES_STEP, explore_set_profile, make_env
from .test_mcp_server import Harness, call, tool_names

pytestmark = pytest.mark.unit

CODES = {"r": "Red", "g": "Green", "b": "Blue"}


def learn_with_codes(env: Any) -> dict[str, Any]:
    eid = explore_set_profile(env)
    return env.skills.learn(
        eid,
        name="Set profile",
        description="Sets notes and colour",
        skill_id="set-profile",
        inputs={
            "notes": {"example": "hello"},
            "colour": {"type": "enum", "value_map": CODES, "example": "b"},
        },
        bindings={"notes": [NOTES_STEP], "colour": [COLOUR_STEP]},
    )


def test_value_map_accepts_codes_and_maps_to_ui_literals(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    learned = learn_with_codes(env)
    assert learned["learning_params"]["colour"] == "Blue", (
        "literal is the UI text of the example code"
    )
    wf = env.store.workflow("set-profile", 1)
    assert wf.steps[1].value == "{{colour}}"
    r = env.skills.run("set-profile", {"notes": "x", "colour": "g"})
    assert r["verified"] and env.profile()["colour"] == "Green"
    bad = env.skills.run("set-profile", {"notes": "x", "colour": "Green"})
    assert bad["failure"]["category"] == "bad_parameters", "only the codes are accepted"


def test_preconditions_stop_before_any_input(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    learn_with_codes(env)
    wf = env.store.workflow("set-profile", 1)
    assert wf.preconditions and wf.preconditions[0].type == "ui_element_exists"
    broken = wf.model_copy(
        update={
            "preconditions": [
                Expectation(
                    type="ui_element_exists",
                    selector=SelectorSpec(automation_id="Nope"),
                    timeout_ms=0,
                )
            ]
        }
    )
    calls = len(env.adapter.calls)
    res = run_workflow(
        env.adapter, env.adapter.attach(env.fake.app_id), broken, {"notes": "x", "colour": "r"}
    )
    assert res.failure is not None and res.failure.category == "precondition_failed"
    assert res.action_count == 0 and len(env.adapter.calls) == calls


def test_positional_selector_for_unnamed_control() -> None:
    trace = [
        {
            "index": 0,
            "ok": True,
            "action": {
                "type": "set_value",
                "selector": {"role": "ComboBox", "index": 2},
                "value": "Japanese",
            },
            "element": {"role": "ComboBox", "name": None},
        }
    ]
    verifications = [
        {
            "after_step": 1,
            "expectation": {
                "type": "ui_value_equals",
                "selector": {"role": "ComboBox", "index": 2},
                "expected": "Japanese",
            },
            "result": {"passed": True},
        }
    ]
    wf, _ = extract_workflow(
        execution={"execution_id": "e1", "trace": trace, "verifications": verifications},
        skill_id="set-mic",
        app_key="x",
        name="Set mic",
        description="d",
        inputs={"language": {"example": "Japanese"}},
        bindings={"language": [0]},
    )
    sel = wf.steps[0].selector
    assert sel is not None and sel.role == "ComboBox" and sel.index == 2 and sel.name is None


def test_label_selector_finds_control_after_its_label() -> None:
    from agentify.adapters.base import Element
    from agentify.adapters.base.selectors import label_matches, strategies

    tree = [
        Element(role="Text", name="Your language"),
        Element(role="ComboBox", value="Korean"),
        Element(role="Text", name="Advanced"),
        Element(role="ComboBox", value="Low"),  # an extra combo that shifts positions
        Element(role="Text", name="Microphone Input"),
        Element(role="Button", name="Help"),
        Element(role="ComboBox", value="Japanese"),
    ]
    sel = Selector(role="ComboBox", label="microphone  input")
    assert [s.name for s in strategies(sel)] == ["role_label"]
    assert [e.value for e in label_matches(tree, sel)] == ["Japanese"]
    assert label_matches(tree, Selector(role="ComboBox", label="Nope")) == []
    with pytest.raises(ValueError):
        Selector(label="Microphone Input")  # a label needs a role


def test_label_selector_survives_learning() -> None:
    sel = {"role": "ComboBox", "label": "Microphone Input"}
    trace = [
        {
            "index": 0,
            "ok": True,
            "action": {"type": "set_value", "selector": sel, "value": "Japanese"},
            "element": {"role": "ComboBox", "name": None},
        }
    ]
    verifications = [
        {
            "after_step": 1,
            "expectation": {"type": "ui_value_equals", "selector": sel, "expected": "Japanese"},
            "result": {"passed": True},
        }
    ]
    wf, _ = extract_workflow(
        execution={"execution_id": "e1", "trace": trace, "verifications": verifications},
        skill_id="set-mic",
        app_key="x",
        name="Set mic",
        description="d",
        inputs={"language": {"example": "Japanese"}},
        bindings={"language": [0]},
    )
    step = wf.steps[0].selector
    assert step is not None and step.label == "Microphone Input" and step.index is None
    assert wf.verification[0].selector is not None
    assert wf.verification[0].selector.label == "Microphone Input"


def test_explore_scope_blocks_other_apps(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    env.executions.scope_app_key = "notepad"
    with pytest.raises(InvalidState, match="scoped"):
        env.executions.start(env.fake.app_id, "g")


async def test_explorer_mode_server_is_scoped(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    scoped = build_app(h.adapter, h.settings, h.store, explore_scope="notepad")
    async with Client(scoped.server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert "explore_app" not in names and "perform_action" in names
        assert (await call(c, "list_apps"))["apps"] == [], (
            "other apps are invisible to the explorer"
        )


def repair_harness(tmp_path: Path, **over: Any) -> Harness:
    """Repair and iteration are disabled in the MVP (defaults off); these tests turn them on."""
    from .helpers import settings

    h = Harness(tmp_path)
    flags: dict[str, Any] = {
        "AGENTIFY_AUTO_IMPROVE": True,
        "AGENTIFY_EXPLORE_MAX_REPAIRS": 2,
        "AGENTIFY_ENABLE_IMPROVE_TOOL": True,
    }
    h.settings = settings(tmp_path, **(flags | over))
    h.app = build_app(h.adapter, h.settings, h.store)
    return h


def _fake_explorer(  # type: ignore[no-untyped-def]
    h: Harness, *, break_after_learning: bool = False, can_repair: bool = True
):
    """Stand-in for the claude -p sub-agent: explores via a scoped runtime, learns, reports.

    With a repair `task`, it re-explores the current UI and calls update_skill (as the real
    Explorer is instructed to), unless can_repair is False (it then gives up without a version).
    """
    calls: list[dict[str, Any]] = []

    def run(**kwargs: Any) -> ExplorerReport:
        calls.append(kwargs)
        sub = build_app(h.adapter, h.settings, h.store, explore_scope=kwargs["app_key"])
        repairing = kwargs.get("task") is not None
        if repairing and not can_repair:
            return ExplorerReport(True, 5.0, turns=2, answer="could not find the cause")
        ex = sub.executions.start(kwargs["app_id"], kwargs["goal"])
        try:
            save = h.fake.find_node(automation_id="SaveButton")
        except KeyError:  # renamed by the test
            save = h.fake.find_node(automation_id="StoreButton")
        sub.executions.perform(
            ex.execution_id,
            Action(type="set_value", selector=Selector(role="Edit", name="Notes"), value="hello"),
        )
        sub.executions.perform(
            ex.execution_id,
            Action(
                type="set_value", selector=Selector(role="ComboBox", name="Colour"), value="Blue"
            ),
        )
        sub.executions.perform(
            ex.execution_id,
            Action(type="click", selector=Selector(role="Button", name=save.name, exact=True)),
        )
        sub.executions.verify(
            ex.execution_id,
            Expectation(
                type="ui_value_equals",
                selector=SelectorSpec(automation_id="StatusLabel"),
                property="name",
                expected="Current colour: Blue",
            ),
        )
        inputs = {
            "notes": {"example": "hello"},
            "colour": {"type": "enum", "value_map": CODES, "example": "b"},
        }
        bindings = {"notes": [0], "colour": [1]}
        if repairing:
            sub.skills.update(
                "set-profile", execution_id=ex.execution_id, inputs=inputs, bindings=bindings
            )
        else:
            sub.skills.learn(
                ex.execution_id,
                name="Set profile",
                description="Sets notes and colour",
                skill_id="set-profile",
                inputs=inputs,
                bindings=bindings,
            )
            if break_after_learning:  # the UI changes before publishing
                node = h.fake.find_node(automation_id="SaveButton")
                node.name, node.automation_id = "Store", "StoreButton"
        return ExplorerReport(
            True, 42.0, turns=12, tool_calls=["mcp__agentify-explorer__perform_action"] * 3
        )

    run.calls = calls  # type: ignore[attr-defined]
    return run


async def test_explore_app_publishes_only_after_verified_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    monkeypatch.setattr(explorer_pkg, "run_explorer", _fake_explorer(h))
    async with Client(h.app.server) as c:
        out = await call(
            c,
            "explore_app",
            {"app": "agentify fixture", "goal": "set the profile notes and colour"},
        )
        assert out["published"] == ["set-profile"] and out["explorer"]["ui_actions"] == 3
        assert out["repairs"] == 0
        cap = out["capabilities"][0]
        replays = cap["pre_publish_replay"]
        assert [r["verified"] for r in replays] == [True, True]
        assert replays[0]["params"]["colour"] != "b", "first replay uses another choice"
        assert replays[1]["params"] == {"notes": "hello", "colour": "b"}, "then restores"
        assert h.profile()["colour"] == "Blue", "the goal's state is restored"
        assert cap["status"] == "active", "two verified runs with distinct inputs promote it"
        assert cap["tool_name"] == "powershell__set-profile"
        tools = {t.name for t in (await c.list_tools()).tools}
        assert "powershell__set-profile" in tools
        caps = (await call(c, "list_capabilities", {"app": "powershell"}))["capabilities"]
        stats = caps[0]["stats"]
        assert (
            stats["replays_verified"] >= 1
            and stats["replay_llm_calls"] == 0
            and stats["discovery_ms"]
        )
        r = await call(
            c,
            "execute_capability",
            {"capability_id": "set-profile", "arguments": {"notes": "n", "colour": "g"}},
        )
        assert r["verified"] and h.profile()["colour"] == "Green"


async def test_failed_replay_goes_back_to_the_explorer_which_repairs_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = repair_harness(tmp_path)
    fake = _fake_explorer(h, break_after_learning=True)
    monkeypatch.setattr(explorer_pkg, "run_explorer", fake)
    async with Client(h.app.server) as c:
        out = await call(c, "explore_app", {"app": "powershell", "goal": "set the profile"})
        first, second = out["iterations"]
        assert first["kind"] == "discover" and first["capabilities"][0]["status"] == "rejected"
        assert second["kind"] == "repair" and out["repairs"] == 1
        task = fake.calls[1]["task"]  # the evidence handed to the Explorer
        assert "set-profile" in task and "Save" in task and "selector_unresolved" in task
        assert out["published"] == ["set-profile"]
        cap = out["capabilities"][0]
        assert cap["version"] == 2 and cap["status"] == "active"
        assert "powershell__set-profile" in {t.name for t in (await c.list_tools()).tools}
        r = await call(
            c,
            "execute_capability",
            {"capability_id": "set-profile", "arguments": {"notes": "n", "colour": "r"}},
        )
        assert r["verified"]


async def test_explore_app_rejects_when_repairs_do_not_fix_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = repair_harness(tmp_path)
    monkeypatch.setattr(
        explorer_pkg, "run_explorer", _fake_explorer(h, break_after_learning=True, can_repair=False)
    )
    async with Client(h.app.server) as c:
        out = await call(c, "explore_app", {"app": "powershell", "goal": "set the profile"})
        assert out["published"] == [] and out["note"]
        assert len(out["iterations"]) == 1 + h.settings.explore_max_repairs
        assert out["iterations"][0]["capabilities"][0]["status"] == "rejected"
        assert "powershell__set-profile" not in {t.name for t in (await c.list_tools()).tools}


async def test_improve_capability_repairs_with_the_failing_arguments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = repair_harness(tmp_path, AGENTIFY_AUTO_IMPROVE=False)
    fake = _fake_explorer(h)
    monkeypatch.setattr(explorer_pkg, "run_explorer", fake)
    async with Client(h.app.server) as c:
        await call(c, "explore_app", {"app": "powershell", "goal": "set the profile"})
        node = h.fake.find_node(automation_id="SaveButton")  # the app changes later
        node.name, node.automation_id = "Store", "StoreButton"
        r = await call(
            c,
            "execute_capability",
            {"capability_id": "set-profile", "arguments": {"notes": "n", "colour": "g"}},
        )
        assert not r["verified"] and "improve_capability" in r["next_step"]
        out = await call(
            c,
            "improve_capability",
            {
                "capability_id": "set-profile",
                "problem": "the run failed: Save button not found",
                "failing_arguments": {"notes": "n", "colour": "g"},
            },
        )
        assert out["published"] == ["set-profile"] and out["iterations"][0]["kind"] == "repair"
        replays = out["capabilities"][0]["pre_publish_replay"]
        assert {"notes": "n", "colour": "g"} in [x["params"] for x in replays]
        assert "Save button not found" in fake.calls[-1]["task"]


def test_value_map_validation() -> None:
    from agentify.workflows.models import InputSpec
    from agentify.workflows.template import validate_params

    spec = {"lang": InputSpec(type="enum", value_map={"ja": "Japanese", "ko": "Korean"})}
    assert validate_params(spec, {"lang": "ko"}) == {"lang": "Korean"}
    with pytest.raises(BadParameters):
        validate_params(spec, {"lang": "Korean"})


def test_mid_run_checks_become_step_checks(tmp_path: Path) -> None:
    """Verify, then navigate on (e.g. Back): the check is kept on the step it followed."""
    from .helpers import COMBO, LABEL, NOTES, SAVE

    env = make_env(tmp_path)
    eid = env.executions.start(env.fake.app_id, "set colour then leave").execution_id
    env.executions.perform(eid, Action(type="set_value", selector=NOTES, value="hello"))
    env.executions.perform(eid, Action(type="set_value", selector=COMBO, value="Blue"))
    env.executions.perform(eid, Action(type="click", selector=SAVE, idempotent=False))
    label = Expectation(
        type="ui_value_equals", selector=LABEL, property="name", expected="Current colour: Blue"
    )
    env.executions.verify(eid, label)
    constant = Expectation(
        type="ui_element_exists", selector=SelectorSpec(role="Edit", name="Notes")
    )
    env.executions.verify(eid, constant)  # input-independent: not kept as a step check
    env.executions.perform(eid, Action(type="focus", selector=NOTES))  # "navigate away"
    env.skills.learn(
        eid,
        name="Set colour",
        description="d",
        skill_id="set-colour",
        inputs={
            "notes": {"example": "hello"},
            "colour": {"type": "enum", "value_map": CODES, "example": "b"},
        },
        bindings={"notes": [0], "colour": [1]},
    )
    wf = env.store.workflow("set-colour", 1)
    assert not wf.verification
    assert [len(s.checks) for s in wf.steps] == [0, 0, 1, 0]
    assert wf.steps[2].checks[0].expected == "Current colour: {{colour}}"
    assert env.skills.run("set-colour", {"notes": "x", "colour": "r"})["verified"]
    # A failing step check stops the run right there (the later steps never run).
    steps = list(wf.steps)
    bogus = (
        steps[2].checks[0].model_copy(update={"expected": "Current colour: nope", "timeout_ms": 0})
    )
    steps[2] = steps[2].model_copy(update={"checks": [bogus]})
    res = run_workflow(
        env.adapter,
        env.adapter.attach(env.fake.app_id),
        wf.model_copy(update={"steps": steps}),
        {"notes": "x", "colour": "g"},
    )
    assert res.failure is not None and res.failure.category == "verification_failed"
    assert res.failure.step_id == "s3" and res.action_count == 3


async def test_batched_actions_return_ui_and_inline_checks(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    async with Client(h.app.server) as c:
        apps = (await call(c, "list_apps"))["apps"]
        app_id = next(a["app_id"] for a in apps if a["app_key"] == "powershell")
        ex = await call(c, "start_learning", {"app_id": app_id, "goal": "set profile"})
        assert "Colour" in ex["ui"], "start_learning returns the outline: no inspect turn"
        eid = ex["execution_id"]
        out = await call(
            c,
            "perform_action",
            {
                "execution_id": eid,
                "settle_ms": 0,
                "actions": [
                    {"type": "set_value", "selector": {"role": "Edit", "name": "Notes"}, "value": "hi"},
                    {"type": "set_value", "selector": {"role": "ComboBox", "name": "Colour"}, "value": "Blue"},
                    {"type": "click", "selector": {"role": "Button", "name": "Save", "exact": True}},
                    {"verify": {"type": "ui_value_equals", "selector": {"automation_id": "StatusLabel"},
                                "property": "name", "expected": "Current colour: Blue"}},
                    {"type": "click", "selector": {"role": "Button", "name": "Nope"}},
                    {"type": "click", "selector": {"role": "Button", "name": "Save"}},
                ],
            },
        )  # fmt: skip
        res = out["results"]
        assert [r.get("ok") for r in res[:3]] == [True, True, True]
        assert res[3]["verify"]["passed"] and res[4]["ok"] is False
        assert len(res) == 5, "stops at the first failure"
        assert "Current colour: Blue" in out["ui_after"]
        bad = await call(
            c,
            "learn_skill",
            {"execution_id": eid, "name": "P", "description": "d", "skill_id": "set-p",
             "inputs": {"colour": {"example": "Blue"}}, "bindings": {"colour": [1]},
             "keep_steps": [0, 1, 2],
             "verify": [{"type": "ui_value_equals", "selector": {"automation_id": "StatusLabel"},
                         "property": "name", "expected": "Current colour: Red"}]},
        )  # fmt: skip
        assert bad["learned"] is False and not bad["verification"][0]["passed"]
        good = await call(
            c,
            "learn_skill",
            {"execution_id": eid, "name": "P", "description": "d", "skill_id": "set-p",
             "inputs": {"colour": {"example": "Blue"}}, "bindings": {"colour": [1]},
             "keep_steps": [0, 1, 2],
             "verify": [{"type": "ui_value_equals", "selector": {"automation_id": "StatusLabel"},
                         "property": "name", "expected": "Current colour: Blue"}]},
        )  # fmt: skip
        assert good["skill_id"] == "set-p" and good["tool_name"]


def test_outline_labels_unnamed_controls() -> None:
    from agentify.adapters.base import Element
    from agentify.mcp.server import compact_tree

    tree = (
        Element(
            role="Pane",
            children=(
                Element(role="Text", name="Microphone Input"),
                Element(role="ComboBox", value="Korean"),
                Element(role="ComboBox", value="Low"),  # second combo: not the label's control
                Element(role="Text", name="Volume"),
                Element(role="Slider", value="5"),
            ),
        ),
    )
    lines, _ = compact_tree(tree, 50)
    text = "\n".join(lines)
    assert "ComboBox label='Microphone Input' value='Korean'" in text
    assert "label='Microphone Input' value='Low'" not in text
    assert "Slider label='Volume'" in text
    only, _ = compact_tree(tree, 50, query="volume")
    assert len(only) == 2 and all("Volume" in line for line in only)


def test_outputs_return_values_read_after_verification(tmp_path: Path) -> None:
    from .helpers import NOTES

    env = make_env(tmp_path)
    eid = explore_set_profile(env)
    read = env.executions.perform(eid, Action(type="read_value", selector=NOTES))
    env.skills.learn(
        eid,
        name="Set profile",
        description="d",
        skill_id="set-profile",
        inputs={"notes": {"example": "hello"}, "colour": {"example": "Blue"}},
        bindings={"notes": [NOTES_STEP], "colour": [COLOUR_STEP]},
        outputs={
            "status": {"selector": {"automation_id": "StatusLabel"}, "property": "name"},
            "notes_now": read["index"],  # from a read step in the trace
        },
    )
    wf = env.store.workflow("set-profile", 1)
    assert wf.outputs["status"].property == "name"
    assert wf.outputs["notes_now"].property == "value"
    r = env.skills.run("set-profile", {"notes": "abc", "colour": "Green"})
    assert r["verified"]
    assert r["outputs"] == {"status": "Current colour: Green", "notes_now": "abc"}


def test_negative_index_picks_from_the_end() -> None:
    from agentify.adapters.base import Element
    from agentify.adapters.base.errors import ElementNotFound
    from agentify.adapters.base.selectors import pick

    items = [Element(role="Text", name=n) for n in ("a", "b", "c")]
    assert pick(items, Selector(role="Text", index=-1)).name == "c"
    assert pick(items, Selector(role="Text", index=-3)).name == "a"
    with pytest.raises(ElementNotFound):
        pick(items, Selector(role="Text", index=-4))


async def test_window_allowlist_hides_and_refuses_other_windows(tmp_path: Path) -> None:
    from agentify.adapters.base import PermissionDenied
    from agentify.adapters.fake.scenarios import colour_fixture_app

    from .helpers import settings

    h = Harness(tmp_path)
    other = colour_fixture_app(h.desktop, version="A", state_file=tmp_path / "other.json")
    s = settings(tmp_path, AGENTIFY_ALLOWED_WINDOWS=h.fake.app_id)
    app = build_app(h.adapter, s, h.store)
    async with Client(app.server) as c:
        ids = [a["app_id"] for a in (await call(c, "list_apps"))["apps"]]
        assert ids == [h.fake.app_id], "only the allowed window is visible"
        r = await c.call_tool("start_learning", {"app_id": other.app_id, "goal": "x"})
        assert r.is_error and "ALLOWED_WINDOWS" in r.content[0].text  # type: ignore[union-attr]
    with pytest.raises(PermissionDenied):
        app.adapter.attach(other.app_id)


def test_optional_steps_are_skipped_when_their_element_is_missing(tmp_path: Path) -> None:
    from agentify.errors import LearnError

    from .helpers import SAVE_STEP

    env = make_env(tmp_path)
    eid = explore_set_profile(env)
    env.skills.learn(
        eid,
        name="Set profile",
        description="d",
        skill_id="set-profile",
        inputs={"notes": {"example": "hello"}, "colour": {"example": "Blue"}},
        bindings={"notes": [NOTES_STEP], "colour": [COLOUR_STEP]},
        optional_steps=[SAVE_STEP],
    )
    wf = env.store.workflow("set-profile", 1)
    assert [s.optional for s in wf.steps] == [False, False, True]
    steps = list(wf.steps)
    steps[2] = steps[2].model_copy(update={"selector": SelectorSpec(automation_id="Gone")})
    res = run_workflow(
        env.adapter,
        env.adapter.attach(env.fake.app_id),
        wf.model_copy(update={"steps": steps}),
        {"notes": "x", "colour": "Red"},
    )
    assert res.steps[2].ok is False and res.action_count == 3, "the run went on past it"
    assert res.failure is not None and res.failure.category == "verification_failed"
    with pytest.raises(LearnError, match="optional_steps"):
        eid2 = explore_set_profile(env)
        env.skills.learn(
            eid2,
            name="x",
            description="d",
            skill_id="other",
            inputs={"notes": {"example": "hello"}},
            bindings={"notes": [NOTES_STEP]},
            optional_steps=[0],  # a failed action in the trace
        )


async def test_a_failed_run_is_repaired_automatically_within_the_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = repair_harness(tmp_path)
    fake = _fake_explorer(h)
    monkeypatch.setattr(explorer_pkg, "run_explorer", fake)
    async with Client(h.app.server) as c:
        await call(c, "explore_app", {"app": "powershell", "goal": "set the profile"})
        node = h.fake.find_node(automation_id="SaveButton")  # the app changes later
        node.name, node.automation_id = "Store", "StoreButton"
        r = await call(
            c,
            "execute_capability",
            {"capability_id": "set-profile", "arguments": {"notes": "n", "colour": "g"}},
        )
        assert r["verified"] and r["version"] == 2
        assert r["improved"]["first_attempt"]["failure"]["category"] == "selector_unresolved"
        assert fake.calls[-1]["task"] is not None, "the Explorer got a repair task"
        assert h.profile() == {"colour": "Green", "notes": "n", "version": "A"}, (
            "the caller's arguments ran last"
        )


async def test_a_flaky_failure_is_retried_not_repaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = repair_harness(tmp_path)
    fake = _fake_explorer(h)
    monkeypatch.setattr(explorer_pkg, "run_explorer", fake)
    async with Client(h.app.server) as c:
        await call(c, "explore_app", {"app": "powershell", "goal": "set the profile"})
        real_run = h.app.skills.run
        calls = {"n": 0}

        def flaky_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls["n"] += 1
            if calls["n"] == 1:  # one transient failure, as if the UI was briefly busy
                return {
                    "verified": False,
                    "promoted_to_active": False,
                    "failure": {"category": "timeout", "step_id": "s2", "message": "busy"},
                }
            return real_run(*args, **kwargs)

        monkeypatch.setattr(h.app.skills, "run", flaky_run)
        explorer_calls = len(fake.calls)
        r = await call(
            c,
            "execute_capability",
            {"capability_id": "set-profile", "arguments": {"notes": "n", "colour": "r"}},
        )
        assert r["verified"] and r["flaky"]["first_attempt"]["failure"]["category"] == "timeout"
        assert len(fake.calls) == explorer_calls, "no repair for a flaky moment"
        assert len(h.store.version_rows("set-profile")) == 1, "no new version"


async def test_mvp_default_has_no_repair_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MVP scope: explore, reuse, explore again. A failed run is reported, not repaired."""
    h = Harness(tmp_path)
    fake = _fake_explorer(h)
    monkeypatch.setattr(explorer_pkg, "run_explorer", fake)
    async with Client(h.app.server) as c:
        assert "improve_capability" not in await tool_names(c)
        await call(c, "explore_app", {"app": "powershell", "goal": "set the profile"})
        node = h.fake.find_node(automation_id="SaveButton")
        node.name, node.automation_id = "Store", "StoreButton"
        calls = len(fake.calls)
        r = await call(
            c,
            "execute_capability",
            {"capability_id": "set-profile", "arguments": {"notes": "n", "colour": "g"}},
        )
        assert not r["verified"] and r["failure"]["category"] == "selector_unresolved"
        assert "next_step" not in r and "improved" not in r
        assert len(fake.calls) == calls, "no Explorer run"
        assert len(h.store.version_rows("set-profile")) == 1


async def test_a_capability_that_consumed_its_start_state_is_published_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E.g. "log out": right after the exploration the goal state holds, so a replay can neither
    run nor prove anything. It is published (candidate) with the skip recorded."""
    h = Harness(tmp_path)
    marks: dict[str, int] = {}

    def run(**kwargs: Any) -> ExplorerReport:
        sub = build_app(h.adapter, h.settings, h.store, explore_scope=kwargs["app_key"])
        ex = sub.executions.start(kwargs["app_id"], kwargs["goal"])
        sub.executions.perform(
            ex.execution_id,
            Action(
                type="set_value", selector=Selector(role="ComboBox", name="Colour"), value="Blue"
            ),
        )
        sub.executions.perform(
            ex.execution_id,
            Action(type="click", selector=Selector(role="Button", name="Save", exact=True)),
        )
        sub.executions.verify(
            ex.execution_id,
            Expectation(
                type="ui_value_equals",
                selector=SelectorSpec(automation_id="StatusLabel"),
                property="name",
                expected="Current colour: Blue",
            ),
        )
        sub.skills.learn(
            ex.execution_id, name="Make it blue", description="d", skill_id="make-blue"
        )
        marks["after_explorer"] = len(h.adapter.calls)
        return ExplorerReport(True, 3.0, turns=3)

    monkeypatch.setattr(explorer_pkg, "run_explorer", run)
    async with Client(h.app.server) as c:
        out = await call(c, "explore_app", {"app": "powershell", "goal": "make the profile blue"})
        assert "after_explorer" in marks, out["explorer"]
        calls_before = marks["after_explorer"]
        assert out["published"] == ["make-blue"]
        replay = out["capabilities"][0]["pre_publish_replay"]
        assert replay[0]["skipped"].startswith("the goal state already holds")
        assert not any(a.type in ("click", "set_value") for a in h.adapter.calls[calls_before:]), (
            "no input was sent: the check is read-only"
        )
        assert "powershell__make-blue" in await tool_names(c)
