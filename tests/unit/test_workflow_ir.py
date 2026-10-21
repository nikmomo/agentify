"""Workflow IR: templating, input validation, model validation, YAML round trip."""

from __future__ import annotations

from typing import Any

import pytest

from agentify.errors import BadParameters, WorkflowInvalid
from agentify.workflows import InputSpec, dump_workflow, load_workflow_text, parse_workflow
from agentify.workflows.template import render, template_refs, validate_params

pytestmark = pytest.mark.unit


def wf_data(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "set-colour",
        "app_key": "fixture",
        "version": 1,
        "name": "Set colour",
        "description": "Sets the colour",
        "inputs": {"colour": {"type": "enum", "enum": ["Red", "Green", "Blue"], "example": "Blue"}},
        "steps": [
            {
                "id": "s1",
                "action": "set_value",
                "selector": {"automation_id": "ColourCombo"},
                "value": "{{colour}}",
            },
            {
                "id": "s2",
                "action": "click",
                "selector": {"role": "Button", "name": "Save", "exact": True},
            },
        ],
        "verification": [
            {
                "type": "ui_value_equals",
                "selector": {"automation_id": "StatusLabel"},
                "property": "name",
                "expected": "Current colour: {{colour}}",
            }
        ],
    }
    data.update(over)
    return data


def test_render_is_single_pass_and_literal() -> None:
    assert template_refs("a {{x}} b {{y_2}} {{Bad}}") == {"x", "y_2"}
    # A substituted value that looks like a template is not expanded again.
    assert render("{{x}}-{{y}}", {"x": "{{y}}", "y": "2"}) == "{{y}}-2"
    assert render(None, {}) is None


def test_validate_params_types_enum_pattern() -> None:
    inputs = {
        "colour": InputSpec(type="enum", enum=["Red", "Blue"]),
        "count": InputSpec(type="integer"),
        "flag": InputSpec(type="boolean", required=False),
        "code": InputSpec(pattern=r"[A-Z]{3}", required=False),
    }
    assert validate_params(inputs, {"colour": "Red", "count": 3, "flag": True}) == {
        "colour": "Red",
        "count": "3",
        "flag": "true",
    }
    assert validate_params(inputs, {"colour": "Blue", "count": "7"})["count"] == "7"
    for bad in (
        {"count": 1},  # missing colour
        {"colour": "Pink", "count": 1},
        {"colour": "Red", "count": "x"},
        {"colour": "Red", "count": True},
        {"colour": "Red", "count": 1, "flag": "yes"},
        {"colour": "Red", "count": 1, "code": "ab"},
        {"colour": "Red", "count": 1, "extra": 1},
    ):
        with pytest.raises(BadParameters):
            validate_params(inputs, bad)


def test_valid_workflow_round_trips_through_yaml() -> None:
    wf = parse_workflow(wf_data())
    assert wf.template_refs() == {"colour"}
    again = load_workflow_text(dump_workflow(wf))
    assert again == wf


@pytest.mark.parametrize(
    ("override", "fragment"),
    [
        ({"id": "Bad Id"}, "skill id"),
        ({"steps": []}, "at least one step"),
        ({"verification": []}, "at least one verification"),
        ({"inputs": {}}, "undeclared inputs"),
        (
            {"steps": [{"id": "s1", "action": "read_value", "selector": {"name": "x"}}]},
            "not a workflow step",
        ),
        (
            {
                "steps": [
                    {"id": "s1", "action": "click", "selector": {"name": "{{colour}}"}},
                    {"id": "s1", "action": "click", "selector": {"name": "Save"}},
                ]
            },
            "unique",
        ),
        ({"unexpected": 1}, "unexpected"),
        ({"inputs": {"Colour": {"type": "string"}}}, "input name"),
    ],
)
def test_invalid_workflows_are_rejected(override: dict[str, Any], fragment: str) -> None:
    with pytest.raises(WorkflowInvalid) as info:
        parse_workflow(wf_data(**override))
    assert fragment in str(info.value)


def test_send_keys_deny_list_applies_to_workflows() -> None:
    data = wf_data()
    data["steps"].append({"id": "s3", "action": "send_keys", "keys": "alt+f4"})
    with pytest.raises(WorkflowInvalid):
        parse_workflow(data)


def test_expectation_field_requirements() -> None:
    for exp in (
        {"type": "ui_value_equals", "expected": "x"},
        {"type": "ui_value_equals", "selector": {"name": "x"}},
        {"type": "file_exists"},
    ):
        with pytest.raises(WorkflowInvalid):
            parse_workflow(wf_data(verification=[exp]))


def test_yaml_loader_rejects_non_mapping_and_unsafe_tags() -> None:
    with pytest.raises(WorkflowInvalid):
        load_workflow_text("- just\n- a list\n")
    import yaml

    with pytest.raises(yaml.YAMLError):
        load_workflow_text("!!python/object/apply:os.system ['echo hi']")
