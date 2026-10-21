"""JSON-lines logging with an execution_id carried in a context variable.

Logs go to stderr: the MCP stdio transport owns stdout, so nothing here may print to stdout.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_execution_id: ContextVar[str | None] = ContextVar("agentify_execution_id", default=None)

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def current_execution_id() -> str | None:
    return _execution_id.get()


@contextmanager
def execution_context(execution_id: str) -> Iterator[None]:
    token = _execution_id.set(execution_id)
    try:
        yield
    finally:
        _execution_id.reset(token)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        execution_id = getattr(record, "execution_id", None) or current_execution_id()
        if execution_id:
            payload["execution_id"] = execution_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key != "execution_id":
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger("agentify")
    root.setLevel(level.upper())
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("agentify") else f"agentify.{name}")
