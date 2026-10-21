"""Backend-independent selector semantics shared by all adapters.

Each adapter produces candidate elements however it can; these helpers decide which candidates
match a selector and which single element is chosen, so resolution behaves identically on the
real Windows adapter and the FakeAdapter.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from agentify.adapters.base.errors import AmbiguousSelector, ElementNotFound
from agentify.adapters.base.models import Element, Selector

_WS = re.compile(r"\s+")


def normalize_text(text: str | None) -> str:
    return _WS.sub(" ", (text or "")).strip().casefold()


@dataclass(frozen=True)
class Strategy:
    """One resolution attempt: a name (reported as `resolved_by`) and the reduced selector it uses."""

    name: str
    selector: Selector


def strategies(selector: Selector) -> list[Strategy]:
    """Ordered resolution strategies: automation_id -> slug -> role+name."""
    out: list[Strategy] = []
    root, index = selector.root, selector.index
    if selector.automation_id:
        out.append(
            Strategy(
                "automation_id",
                Selector(automation_id=selector.automation_id, root=root, index=index),
            )
        )
    if selector.slug:
        out.append(
            Strategy(
                "slug", Selector(slug=selector.slug, role=selector.role, root=root, index=index)
            )
        )
    if not (selector.automation_id or selector.slug or selector.name) and selector.role:
        if selector.label:
            # Unnamed control identified by the visible label that precedes it.
            out.append(
                Strategy(
                    "role_label",
                    Selector(role=selector.role, label=selector.label, root=root, index=index),
                )
            )
        else:
            # Unnamed control: the index-th element of this role, in tree order.
            out.append(Strategy("role_index", Selector(role=selector.role, root=root, index=index)))
    if selector.name:
        out.append(
            Strategy(
                "role_name",
                Selector(
                    role=selector.role,
                    name=selector.name,
                    exact=selector.exact,
                    root=root,
                    index=index,
                ),
            )
        )
    return out


def element_matches(element: Element, selector: Selector) -> bool:
    """True if the element satisfies every identity field set on the selector (root/index ignored)."""
    if selector.automation_id is not None and element.automation_id != selector.automation_id:
        return False
    if selector.slug is not None and element.slug != selector.slug:
        return False
    if selector.role is not None and normalize_text(element.role) != normalize_text(selector.role):
        return False
    if selector.name is not None:
        want = normalize_text(selector.name)
        have = normalize_text(element.name)
        if selector.exact:
            if have != want:
                return False
        elif want not in have:
            return False
    return True


def label_matches(ordered: list[Element], selector: Selector) -> list[Element]:
    """Elements of `selector.role` that follow (in tree order) an element named `selector.label`.

    Returns the first such element after each label occurrence (normally one). The label text is
    compared after whitespace/case normalization, exactly.
    """
    want = normalize_text(selector.label)
    role = normalize_text(selector.role)
    out: list[Element] = []
    for i, el in enumerate(ordered):
        if normalize_text(el.name) != want:
            continue
        for nxt in ordered[i + 1 :]:
            if normalize_text(nxt.role) == role:
                if nxt not in out:
                    out.append(nxt)
                break
    return out


def filter_matches(elements: Iterable[Element], selector: Selector) -> list[Element]:
    return [e for e in elements if element_matches(e, selector)]


def pick(matches: list[Element], selector: Selector) -> Element:
    """Choose exactly one element, honouring `index`; raise typed errors otherwise."""
    if not matches:
        raise ElementNotFound(f"no element matches ({selector.describe()})")
    if selector.index is not None:
        if not -len(matches) <= selector.index < len(matches):
            raise ElementNotFound(
                f"index {selector.index} out of range; {len(matches)} matches for ({selector.describe()})"
            )
        return matches[selector.index]
    if len(matches) > 1:
        raise AmbiguousSelector(
            f"{len(matches)} elements match ({selector.describe()}); set index or narrow the selector",
            details={"candidates": [_summary(e) for e in matches[:10]]},
        )
    return matches[0]


def _summary(e: Element) -> dict[str, object]:
    return {"role": e.role, "name": e.name, "automation_id": e.automation_id, "slug": e.slug}
