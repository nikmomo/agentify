"""Runtime-level errors (above the adapter). Adapter failures stay `AdapterError`."""

from __future__ import annotations

from typing import Any


class AgentifyError(Exception):
    """Typed error surfaced to MCP clients as {"error": {code, message, details}}."""

    code = "agentify_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class BadParameters(AgentifyError):
    code = "bad_parameters"


class NotFound(AgentifyError):
    code = "not_found"


class InvalidState(AgentifyError):
    code = "invalid_state"


class LimitExceeded(AgentifyError):
    code = "limit_exceeded"


class ApprovalRequired(AgentifyError):
    code = "approval_required"


class WorkflowInvalid(AgentifyError):
    code = "workflow_invalid"


class LearnError(AgentifyError):
    code = "learn_error"
