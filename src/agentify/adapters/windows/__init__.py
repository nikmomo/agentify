"""Windows adapter over Microsoft WinApp CLI (`winapp ui ...`)."""

from agentify.adapters.windows.adapter import WinAppCliAdapter
from agentify.adapters.windows.runner import CliCall, CliRunner, WinAppCliRunner

__all__ = ["CliCall", "CliRunner", "WinAppCliAdapter", "WinAppCliRunner"]
