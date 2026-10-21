"""Secret references: `${secret:NAME}` in action values, keys, and skill inputs.

Agents, traces, workflows, parameter sets, and logs only ever contain the reference. The value
is read from the environment (or the secrets file, `.env` by default) at the moment an action is
sent to the app, and only for names listed in AGENTIFY_SECRET_NAMES. Secrets cannot be verified
or read back, so their values never appear in a result either.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from dotenv import dotenv_values

from agentify.errors import BadParameters

SECRET_REF = re.compile(r"\$\{secret:([A-Za-z_][A-Za-z0-9_]*)\}")


def has_secret(text: str | None) -> bool:
    return bool(text) and SECRET_REF.search(text or "") is not None


def is_secret_ref(text: str) -> bool:
    return SECRET_REF.fullmatch(text.strip()) is not None


class SecretResolver:
    def __init__(self, allowed_names: str, secrets_file: Path | None = None) -> None:
        self.allowed = frozenset(n.strip() for n in allowed_names.split(",") if n.strip())
        self.secrets_file = secrets_file

    def _value(self, name: str) -> str:
        if name not in self.allowed:
            raise BadParameters(
                f"secret {name!r} is not allowed; list it in AGENTIFY_SECRET_NAMES",
                details={"secret": name},
            )
        value = os.environ.get(name)
        if not value and self.secrets_file is not None and self.secrets_file.is_file():
            value = dotenv_values(self.secrets_file).get(name)
        if not value:
            raise BadParameters(f"secret {name!r} is not set", details={"secret": name})
        return value

    def known_values(self) -> dict[str, str]:
        """{value: NAME} for every allowed secret that is set (to keep values out of records)."""
        out = {}
        for name in self.allowed:
            try:
                out[self._value(name)] = name
            except BadParameters:
                continue
        return out

    def mask(self, text: str | None) -> str | None:
        """Replace any known secret value in `text` with its ${secret:NAME} reference."""
        if not text:
            return text
        for value, name in sorted(self.known_values().items(), key=lambda kv: -len(kv[0])):
            if len(value) >= 4 and value in text:
                text = text.replace(value, "${secret:" + name + "}")
        return text

    def resolve(self, text: str | None) -> str | None:
        """Replace every reference with its value (call this only right before input is sent)."""
        if not text or not has_secret(text):
            return text
        return SECRET_REF.sub(lambda m: self._value(m.group(1)), text)
