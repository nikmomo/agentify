"""Keyboard safety policy, enforced by every adapter before any key reaches the backend.

Verified 2026-10-15: WinApp CLI 0.7.1 accepted `send-keys "alt+f4"` with the default post-message
transport (exit 0) and closed the target window, although its docs say system combos are rejected
without --allow-system-keys. Agentify therefore enforces its own deny list and never passes
--allow-system-keys.
"""

from __future__ import annotations

from agentify.adapters.base.errors import PermissionDenied

_ALIASES = {
    "control": "ctrl",
    "ctl": "ctrl",
    "escape": "esc",
    "delete": "del",
    "windows": "win",
    "lwin": "win",
    "rwin": "win",
    "meta": "win",
    "super": "win",
    "cmd": "win",
    "menu": "alt",
}

_DENIED_COMBOS: frozenset[frozenset[str]] = frozenset(
    frozenset(c.split("+"))
    for c in (
        "alt+f4",
        "alt+tab",
        "alt+esc",
        "alt+space",
        "ctrl+esc",
        "ctrl+alt+del",
        "ctrl+shift+esc",
        "ctrl+alt+tab",
    )
)

# Virtual-key codes for the Windows keys and Apps/Sleep keys.
_DENIED_VK = frozenset({0x5B, 0x5C, 0x5D, 0x5F})


def _parts(token: str) -> frozenset[str]:
    return frozenset(_ALIASES.get(p, p) for p in token.lower().split("+") if p)


def denied_reason(keys: str, *, verbatim: bool = False) -> str | None:
    """Return why `keys` is denied, or None if allowed. Verbatim input is literal text and allowed."""
    if verbatim:
        return None
    for token in keys.split():
        low = token.lower()
        if low.startswith("text="):
            continue
        if low.startswith("vk="):
            try:
                code = int(low[3:], 0)
            except ValueError:
                return f"unparseable virtual key {token!r}"
            if code in _DENIED_VK:
                return f"virtual key {token!r} is a system key"
            continue
        parts = _parts(low)
        if "win" in parts:
            return f"{token!r} uses the Windows key"
        if parts in _DENIED_COMBOS:
            return f"{token!r} is a system key combination"
    return None


def check_keys(keys: str, *, verbatim: bool = False) -> None:
    reason = denied_reason(keys, verbatim=verbatim)
    if reason:
        raise PermissionDenied(f"send_keys blocked: {reason}", details={"keys": keys})


def redact_keys(keys: str) -> str:
    """Hide literal text payloads in logs while keeping key names visible."""
    return " ".join(
        f"text=<{len(t) - 5} chars>" if t.lower().startswith("text=") else t for t in keys.split()
    )
