"""Key syntax of the in-process keyboard path (parsing only; no input is sent).

It must match WinApp CLI's send-keys syntax, which the CLI path and learned workflows use.
"""

from __future__ import annotations

import pytest

from agentify.adapters.windows.uia import UiaUnavailable, key_events

pytestmark = pytest.mark.unit

UP, UNI = 0x0002, 0x0004


def downs(keys: str) -> list[tuple[int, int]]:
    return [(vk, scan) for vk, scan, flags in key_events(keys, verbatim=False) if not flags & UP]


def test_combo_presses_modifiers_around_the_key() -> None:
    assert key_events("ctrl+s", verbatim=False) == [
        (0x11, 0, 0),
        (ord("S"), 0, 0),
        (ord("S"), 0, UP),
        (0x11, 0, UP),
    ]


def test_text_and_verbatim_are_unicode_characters() -> None:
    assert downs("text=Hé") == [(0, ord("H")), (0, ord("é"))]
    assert len(key_events("a b", verbatim=True)) == 6  # three characters, the space included


def test_named_and_function_keys() -> None:
    assert downs("enter") == [(0x0D, 0)]
    assert downs("f5") == [(0x74, 0)]


def test_words_that_are_not_keys_are_typed_like_the_cli_does() -> None:
    # WinApp CLI: any token that is not a named key, combo, or vk= is literal text.
    assert downs("hello")[0] == (0, ord("h"))
    assert downs("ctrl+mediaplay")[0] == (0, ord("c"))


def test_text_escapes() -> None:
    # \s space, \t the Tab key, \n the Enter key, \\ a backslash
    assert downs(r"text=a\sb\tc\n\\") == [
        (0, ord("a")),
        (0, ord(" ")),
        (0, ord("b")),
        (0x09, 0),
        (0, ord("c")),
        (0x0D, 0),
        (0, ord("\\")),
    ]
    assert downs(r"end text=\sdef")[:2] == [(0x23, 0), (0, ord(" "))]


def test_characters_outside_the_bmp_use_the_cli() -> None:
    with pytest.raises(UiaUnavailable):
        key_events("text=" + chr(0x1F600), verbatim=False)
