from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agentify.adapters.base import PermissionDenied, RiskLevel
from agentify.adapters.base.keys import check_keys, denied_reason, redact_keys
from agentify.config import load_settings
from agentify.observability.logging import JsonFormatter, execution_context

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "keys",
    [
        "alt+f4",
        "ALT+F4",
        "win+r",
        "lwin+d",
        "ctrl+alt+delete",
        "ctrl+shift+esc",
        "control+escape",
        "text=hello alt+tab",
        "vk=0x5B",
        "alt+space",
    ],
)
def test_system_keys_denied(keys: str) -> None:
    assert denied_reason(keys) is not None
    with pytest.raises(PermissionDenied):
        check_keys(keys)


@pytest.mark.parametrize(
    "keys", ["ctrl+s", "enter", "text=alt+f4", "ctrl+shift+s", "tab tab enter", "vk=0x41", "f4"]
)
def test_ordinary_keys_allowed(keys: str) -> None:
    assert denied_reason(keys) is None


def test_verbatim_text_is_never_a_combo() -> None:
    assert denied_reason("alt+f4", verbatim=True) is None


def test_redact_keys_hides_text() -> None:
    assert redact_keys("ctrl+a text=secret enter") == "ctrl+a text=<6 chars> enter"


def test_settings_precedence(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "AGENTIFY_LOG_LEVEL=DEBUG\nAGENTIFY_WINAPP_TIMEOUT_MS=5000\nAGENTIFY_APPROVAL_THRESHOLD=destructive\nAGENTIFY_WINAPP_PATH=\n"
    )
    s = load_settings(env, environ={"AGENTIFY_WINAPP_TIMEOUT_MS": "7000", "UNRELATED": "x"})
    assert s.log_level == "DEBUG"
    assert s.winapp_timeout_ms == 7000
    assert s.approval_threshold is RiskLevel.DESTRUCTIVE
    assert s.winapp_path is None


def test_settings_defaults_without_file(tmp_path: Path) -> None:
    s = load_settings(tmp_path / "missing.env", environ={})
    assert s.log_level == "INFO"
    assert s.default_send_keys_via == "send-input"


def test_json_log_carries_execution_id() -> None:
    record = logging.LogRecord("agentify.test", logging.INFO, __file__, 1, "hello", (), None)
    record.args_extra = "x"
    with execution_context("exec_123"):
        line = json.loads(JsonFormatter().format(record))
    assert line["execution_id"] == "exec_123"
    assert line["msg"] == "hello"
    assert line["args_extra"] == "x"
