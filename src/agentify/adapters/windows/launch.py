"""Open a desktop app the way a person would: from its Start-menu shortcut.

Only apps registered in the Start menu (the user's and all users' Programs folders) can be opened:
no command lines, no arbitrary paths. If the app is already running, nothing is launched.
"""

from __future__ import annotations

import os
from pathlib import Path


def start_menu_dirs() -> list[Path]:
    dirs = []
    for env in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env)
        if base:
            dirs.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return [d for d in dirs if d.is_dir()]


def find_shortcut(name: str) -> Path | None:
    """The Start-menu shortcut whose name matches `name` (exact first, then contains), if any."""
    want = name.strip().casefold()
    if not want:
        return None
    links = [p for d in start_menu_dirs() for p in d.rglob("*.lnk")]
    exact = [p for p in links if p.stem.casefold() == want]
    if exact:
        return exact[0]
    partial = sorted((p for p in links if want in p.stem.casefold()), key=lambda p: len(p.stem))
    return partial[0] if partial else None


def launch_shortcut(link: Path) -> None:
    os.startfile(str(link))  # Windows only; as a double-click would
