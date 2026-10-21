from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from agentify.adapters.windows.runner import CliCall, map_error, redact_args

RECORDED = Path(__file__).resolve().parents[1] / "fixtures" / "winapp_json"


def recorded(name: str) -> dict[str, Any]:
    return json.loads((RECORDED / f"{name}.json").read_text(encoding="utf-8"))


@dataclass
class Rule:
    command: str
    fixture: str
    contains: tuple[str, ...] = ()
    times: int | None = None


@dataclass
class ScriptedRunner:
    """Replays recorded CLI payloads; applies the same exit-code/error semantics as WinAppCliRunner."""

    rules: list[Rule] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    def on(
        self, command: str, fixture: str, *contains: str, times: int | None = None
    ) -> ScriptedRunner:
        self.rules.append(Rule(command, fixture, contains, times))
        return self

    def run(
        self, args: Sequence[str], *, timeout_ms: int | None = None, allow_nonzero: bool = False
    ) -> CliCall:
        argv = list(args)
        self.calls.append(argv)
        for rule in self.rules:
            if (
                argv[0] == rule.command
                and all(c in argv for c in rule.contains)
                and rule.times != 0
            ):
                if rule.times is not None:
                    rule.times -= 1
                data = recorded(rule.fixture)
                stderr = data.get("stderr")
                error = stderr.get("error") if isinstance(stderr, dict) else None
                call = CliCall(
                    args=argv,
                    exit_code=int(data["exit"]),
                    duration_ms=1,
                    stdout=json.dumps(data.get("stdout")),
                    stderr=json.dumps(stderr) if stderr else "",
                    payload=data.get("stdout"),
                    error=error,
                    redacted_args=redact_args(argv),
                )
                if call.exit_code != 0 and (error is not None or not allow_nonzero):
                    raise map_error(call)
                return call
        raise AssertionError(f"no scripted response for {argv}")

    def commands(self) -> list[str]:
        return [c[0] for c in self.calls]


@pytest.fixture
def runner() -> ScriptedRunner:
    return ScriptedRunner()
