"""Runtime configuration from environment variables and an optional .env file.

Precedence: real environment > .env file > defaults. Keys mirror .env.example.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field

from agentify.adapters.base.models import RiskLevel


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, populate_by_name=True)

    winapp_path: str | None = Field(None, alias="AGENTIFY_WINAPP_PATH")
    winapp_timeout_ms: int = Field(15_000, alias="AGENTIFY_WINAPP_TIMEOUT_MS", gt=0)
    winapp_startup_grace_ms: int = Field(10_000, alias="AGENTIFY_WINAPP_STARTUP_GRACE_MS", ge=0)
    winapp_pinned_version: str = Field("0.7.1", alias="AGENTIFY_WINAPP_PINNED_VERSION")
    default_send_keys_via: str = Field("send-input", alias="AGENTIFY_SEND_KEYS_VIA")

    max_actions_per_execution: int = Field(40, alias="AGENTIFY_MAX_ACTIONS_PER_EXECUTION", gt=0)
    max_execution_seconds: int = Field(180, alias="AGENTIFY_MAX_EXECUTION_SECONDS", gt=0)
    max_explore_seconds: int = Field(1800, alias="AGENTIFY_MAX_EXPLORE_SECONDS", gt=0)
    promote_after_verified_param_sets: int = Field(
        2, alias="AGENTIFY_PROMOTE_AFTER_VERIFIED_PARAM_SETS", ge=1
    )
    approval_threshold: RiskLevel = Field(RiskLevel.DATA_WRITE, alias="AGENTIFY_APPROVAL_THRESHOLD")

    data_dir: Path = Field(Path("./data"), alias="AGENTIFY_DATA_DIR")
    db_path: Path = Field(Path("./data/agentify.sqlite"), alias="AGENTIFY_DB_PATH")
    skills_dir: Path = Field(Path("./data/skills"), alias="AGENTIFY_SKILLS_DIR")
    screenshots_dir: Path = Field(Path("./data/screenshots"), alias="AGENTIFY_SCREENSHOTS_DIR")

    log_level: str = Field("INFO", alias="AGENTIFY_LOG_LEVEL")
    log_format: str = Field("json", alias="AGENTIFY_LOG_FORMAT")

    claude_path: str = Field("claude", alias="AGENTIFY_CLAUDE_PATH")
    explorer_model: str = Field("claude-opus-5-5", alias="AGENTIFY_EXPLORER_MODEL")
    explorer_effort: str = Field("medium", alias="AGENTIFY_EXPLORER_EFFORT")
    allowed_windows: str = Field("", alias="AGENTIFY_ALLOWED_WINDOWS")
    """Comma-separated app ids / window handles; when set, Agentify sees and acts only on them."""
    secret_names: str = Field("", alias="AGENTIFY_SECRET_NAMES")
    """Names that ${secret:NAME} references may use (comma-separated); others are refused."""
    secrets_file: Path = Field(Path(".env"), alias="AGENTIFY_SECRETS_FILE")
    """Where secret values are read from when they are not in the environment."""
    uia_fast_path: bool = Field(True, alias="AGENTIFY_UIA_FAST_PATH")
    """In-process UI Automation for hot operations; false = WinApp CLI only."""
    # Repair and iteration: implemented, disabled by default.
    auto_improve: bool = Field(False, alias="AGENTIFY_AUTO_IMPROVE")
    """A failed capability run hands the failure to the Explorer at once (low-risk skills only)."""
    explore_max_repairs: int = Field(0, ge=0, le=5, alias="AGENTIFY_EXPLORE_MAX_REPAIRS")
    enable_improve_tool: bool = Field(False, alias="AGENTIFY_ENABLE_IMPROVE_TOOL")
    """Expose improve_capability (Explorer repairs a capability on request)."""


def load_settings(
    env_file: Path | None = None, *, environ: dict[str, str] | None = None
) -> Settings:
    """Load settings. `env_file` defaults to ./.env if it exists. Empty values are treated as unset."""
    path = env_file if env_file is not None else Path(".env")
    merged: dict[str, str] = {}
    if path.is_file():
        for key, val in dotenv_values(path).items():
            if val:
                merged[key] = val
    source = os.environ if environ is None else environ
    merged.update({k: v for k, v in source.items() if v != "" and k in _KNOWN_KEYS})
    return Settings.model_validate(merged)


_KNOWN_KEYS: frozenset[str] = frozenset(
    f.alias for f in Settings.model_fields.values() if f.alias is not None
)
