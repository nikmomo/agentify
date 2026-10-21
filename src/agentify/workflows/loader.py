"""YAML <-> Workflow. `yaml.safe_load` only; validation errors become WorkflowInvalid."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agentify.adapters.base.errors import PermissionDenied
from agentify.errors import WorkflowInvalid
from agentify.workflows.models import Workflow


def parse_workflow(data: dict[str, Any]) -> Workflow:
    try:
        return Workflow.model_validate(data)
    except (ValidationError, PermissionDenied) as exc:
        raise WorkflowInvalid(f"invalid workflow: {exc}") from exc


def load_workflow_text(text: str) -> Workflow:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise WorkflowInvalid("workflow YAML must be a mapping")
    return parse_workflow(data)


def load_workflow(path: Path) -> Workflow:
    return load_workflow_text(path.read_text(encoding="utf-8"))


# Always written even when equal to the default, so a file is self-describing.
_ALWAYS = ("schema_version", "id", "app_key", "version", "status", "name", "description", "risk")


def dump_workflow(workflow: Workflow) -> str:
    """YAML without default-valued noise (defaults are restored on load, so round trips are exact)."""
    full = workflow.model_dump(mode="json", exclude_none=True)
    lean = workflow.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    data = {k: full[k] for k in _ALWAYS} | {k: v for k, v in lean.items() if k not in _ALWAYS}
    # Provenance has a default factory (timestamp); exclude_defaults would silently drop it.
    data["provenance"] = full["provenance"]
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
