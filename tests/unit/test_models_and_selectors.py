from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentify.adapters.base import (
    Action,
    AmbiguousSelector,
    Element,
    ElementNotFound,
    RiskLevel,
    Selector,
)
from agentify.adapters.base.selectors import element_matches, filter_matches, pick, strategies

pytestmark = pytest.mark.unit


def test_selector_requires_identity() -> None:
    with pytest.raises(ValidationError):
        Selector(role="Button")
    assert Selector(name="Save").name == "Save"


def test_strategy_order_and_reduction() -> None:
    sel = Selector(automation_id="SaveButton", slug="btn-save-1a2b", role="Button", name="Save")
    names = [s.name for s in strategies(sel)]
    assert names == ["automation_id", "slug", "role_name"]
    rn = strategies(sel)[2].selector
    assert (
        rn.automation_id is None and rn.slug is None and rn.role == "Button" and rn.name == "Save"
    )


def test_name_matching_substring_vs_exact_casefold() -> None:
    e = Element(role="ComboBox", name="The language coming from desktop audio (game voices)")
    assert element_matches(e, Selector(role="combobox", name="language coming"))
    assert not element_matches(e, Selector(role="ComboBox", name="language coming", exact=True))
    assert element_matches(
        e, Selector(name="THE LANGUAGE COMING FROM DESKTOP AUDIO (game voices)", exact=True)
    )
    assert not element_matches(e, Selector(role="Button", name="language"))


def test_pick_ambiguous_index_and_missing() -> None:
    a, b = Element(role="Text", name="Colour"), Element(role="ComboBox", name="Colour")
    sel = Selector(name="Colour")
    with pytest.raises(AmbiguousSelector) as info:
        pick([a, b], sel)
    assert len(info.value.details["candidates"]) == 2
    assert pick([a, b], Selector(name="Colour", index=1)) is b
    with pytest.raises(ElementNotFound):
        pick([], sel)
    with pytest.raises(ElementNotFound):
        pick([a], Selector(name="Colour", index=3))
    assert filter_matches([a, b], Selector(role="ComboBox", name="Colour")) == [b]


def test_stable_automation_id_and_durable_selector() -> None:
    numeric = Element(role="Text", name="Colour", automation_id="393514", slug="393514")
    named = Element(role="ComboBox", name="Colour", automation_id="ColourCombo")
    unnamed = Element(role="Button", slug="btn-x-0011")
    assert not numeric.has_stable_automation_id
    assert named.has_stable_automation_id
    assert numeric.durable_selector() == Selector(role="Text", name="Colour", exact=True)
    assert named.durable_selector().automation_id == "ColourCombo"
    assert unnamed.durable_selector().slug == "btn-x-0011"


def test_action_validation_and_default_risk() -> None:
    with pytest.raises(ValidationError):
        Action(type="invoke")
    with pytest.raises(ValidationError):
        Action(type="set_value", selector=Selector(name="x"))
    with pytest.raises(ValidationError):
        Action(type="send_keys")
    with pytest.raises(ValidationError):
        Action(type="click", selector=Selector(name="x"), pattern="select")
    assert (
        Action(type="read_value", selector=Selector(name="x")).effective_risk is RiskLevel.READ_ONLY
    )
    assert (
        Action(type="invoke", selector=Selector(name="x")).effective_risk
        is RiskLevel.LOCAL_CONFIGURATION_CHANGE
    )
    assert (
        Action(
            type="invoke", selector=Selector(name="x"), risk=RiskLevel.DESTRUCTIVE
        ).effective_risk
        is RiskLevel.DESTRUCTIVE
    )


def test_risk_ordering() -> None:
    assert RiskLevel.DESTRUCTIVE.at_least(RiskLevel.DATA_WRITE)
    assert not RiskLevel.READ_ONLY.at_least(RiskLevel.LOCAL_CONFIGURATION_CHANGE)


def test_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Selector(name="x", colour="red")  # type: ignore[call-arg]


def test_focus_requires_selector() -> None:
    # WinApp CLI 0.7.1 rejects `focus` without an element ("Required argument missing").
    with pytest.raises(ValueError):
        Action(type="focus")
