"""`{{name}}` substitution: single pass, literal replacement, declared inputs only. No expressions."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from agentify.errors import BadParameters

if TYPE_CHECKING:
    from agentify.workflows.models import InputSpec

TEMPLATE_RE = re.compile(r"\{\{([a-z][a-z0-9_]*)\}\}")


def template_refs(text: str | None) -> set[str]:
    return set(TEMPLATE_RE.findall(text)) if text else set()


def render(text: str | None, params: dict[str, str]) -> str | None:
    """Replace every {{name}} once. Substituted values are never re-scanned."""
    if text is None:
        return None
    return TEMPLATE_RE.sub(lambda m: params[m.group(1)], text)


REDACTED = "<secret>"


def redact_secrets(inputs: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """The parameters with every secret-typed input replaced, for anything that is stored."""
    return {
        k: (REDACTED if getattr(inputs.get(k), "type", None) == "secret" else v)
        for k, v in params.items()
    }


def validate_params(inputs: dict[str, InputSpec], params: dict[str, Any]) -> dict[str, str]:
    """Check params against input specs and return them as strings for substitution."""
    unknown = set(params) - set(inputs)
    if unknown:
        raise BadParameters(
            f"unknown parameters: {sorted(unknown)}", details={"expected": sorted(inputs)}
        )
    out: dict[str, str] = {}
    for name, spec in inputs.items():
        if name not in params or params[name] is None:
            if spec.required:
                raise BadParameters(f"missing required parameter {name!r}")
            continue
        raw = params[name]
        if spec.type == "integer":
            if (
                isinstance(raw, bool)
                or not isinstance(raw, int | str)
                or not str(raw).lstrip("-").isdigit()
            ):
                raise BadParameters(f"parameter {name!r} must be an integer")
            text = str(int(raw))
        elif spec.type == "boolean":
            if not isinstance(raw, bool):
                raise BadParameters(f"parameter {name!r} must be a boolean")
            text = "true" if raw else "false"
        else:
            if not isinstance(raw, str):
                raise BadParameters(f"parameter {name!r} must be a string")
            text = raw
        if spec.type == "secret":
            # A ${secret:NAME} reference, or a value the caller was given (e.g. in its task).
            # Either way the runtime never stores it (redact_secrets).
            if not text:
                raise BadParameters(f"parameter {name!r} (secret) is empty")
            out[name] = text.strip()
            continue
        if spec.value_map is not None:
            if text not in spec.value_map:
                raise BadParameters(
                    f"parameter {name!r} must be one of {sorted(spec.value_map)}",
                    details={"got": text},
                )
            out[name] = spec.value_map[text]
            continue
        if spec.enum is not None and text not in spec.enum:
            raise BadParameters(
                f"parameter {name!r} must be one of {spec.enum}", details={"got": text}
            )
        if spec.pattern is not None and not re.fullmatch(spec.pattern, text):
            raise BadParameters(f"parameter {name!r} does not match pattern {spec.pattern!r}")
        out[name] = text
    return out
