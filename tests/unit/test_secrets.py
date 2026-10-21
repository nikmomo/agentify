"""Secret references: values reach the app, never traces, workflows, or results."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentify.adapters.base import Action, Selector
from agentify.errors import BadParameters
from agentify.workflows.models import Expectation

from .helpers import LABEL, make_env

pytestmark = pytest.mark.unit

NOTES = Selector(role="Edit", name="Notes")
REF = "${secret:TEST_NOTE_SECRET}"


def env_with_secret(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEST_NOTE_SECRET", "hunter2")
    return make_env(tmp_path, AGENTIFY_SECRET_NAMES="TEST_NOTE_SECRET")


def test_exploration_sends_the_value_but_records_the_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = env_with_secret(tmp_path, monkeypatch)
    eid = env.executions.start(env.fake.app_id, "type a secret").execution_id
    env.executions.perform(eid, Action(type="set_value", selector=NOTES, value=REF))
    assert env.adapter.read_value(env.adapter.attach(env.fake.app_id), NOTES) == "hunter2"
    stored = json.dumps(env.store.execution(eid))
    assert "hunter2" not in stored and REF in stored
    with pytest.raises(BadParameters, match="secrets cannot be verified"):
        env.executions.verify(
            eid,
            Expectation(
                type="ui_value_equals", selector=NOTES.model_dump(exclude_none=True), expected=REF
            ),
        )


def test_learned_skill_takes_references_or_values_and_never_stores_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = env_with_secret(tmp_path, monkeypatch)
    eid = env.executions.start(env.fake.app_id, "save a secret note").execution_id
    env.executions.perform(eid, Action(type="set_value", selector=NOTES, value=REF))
    env.executions.perform(
        eid, Action(type="click", selector=Selector(role="Button", name="Save", exact=True))
    )
    env.executions.verify(
        eid,
        Expectation(
            type="ui_value_equals", selector=LABEL, property="name", expected="Current colour: Red"
        ),
    )
    env.skills.learn(
        eid,
        name="Save secret note",
        description="d",
        skill_id="save-secret-note",
        inputs={"note": {"type": "secret", "example": REF}},
        bindings={"note": [0]},
    )
    wf_text = (tmp_path / "skills").rglob("*.yaml").__next__().read_text(encoding="utf-8")
    assert "hunter2" not in wf_text and "{{note}}" in wf_text
    r = env.skills.run("save-secret-note", {"note": REF})
    assert r["verified"] and env.profile()["notes"] == "hunter2"
    assert "hunter2" not in json.dumps(r)
    # A caller may pass the value itself (e.g. it was given in its task): it is used, never stored.
    raw = env.skills.run("save-secret-note", {"note": "hunter3"})
    assert raw["verified"] and env.profile()["notes"] == "hunter3"
    stored = json.dumps(env.store.execution(raw["execution_id"])) + json.dumps(
        [dict(r) for r in env.store.version_rows("save-secret-note")]
    )
    assert "hunter3" not in stored and "<secret>" in stored


def test_names_must_be_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OTHER_SECRET", "x")
    env = make_env(tmp_path, AGENTIFY_SECRET_NAMES="TEST_NOTE_SECRET")
    eid = env.executions.start(env.fake.app_id, "g").execution_id
    with pytest.raises(BadParameters, match="AGENTIFY_SECRET_NAMES"):
        env.executions.perform(
            eid, Action(type="set_value", selector=NOTES, value="${secret:OTHER_SECRET}")
        )
    assert env.store.execution(eid)["trace"] in (None, []), "nothing was sent or recorded"


def test_a_secret_typed_in_clear_is_recorded_as_its_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task may give the password in clear: the app gets it, records only hold the reference."""
    env = env_with_secret(tmp_path, monkeypatch)
    eid = env.executions.start(env.fake.app_id, "type the password").execution_id
    entry = env.executions.perform(eid, Action(type="set_value", selector=NOTES, value="hunter2"))
    assert env.adapter.read_value(env.adapter.attach(env.fake.app_id), NOTES) == "hunter2"
    assert entry["action"]["value"] == REF
    assert "hunter2" not in json.dumps(env.store.execution(eid))


def test_allowlist_by_process_name() -> None:
    from agentify.adapters.base.allowlist import WindowAllowlistAdapter, parse_allowlist
    from agentify.adapters.fake import FakeAdapter

    w = WindowAllowlistAdapter(FakeAdapter(), parse_allowlist("my-app"))
    assert w._ok("my-app:123", 123) and w._ok("My-App:999", 999)
    assert not w._ok("notepad:5", 5)
