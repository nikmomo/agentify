"""Platform adapters. Only the Windows adapter (WinApp CLI) and the test FakeAdapter are functional."""

from agentify.adapters.registry import create_adapter, platform_support

__all__ = ["create_adapter", "platform_support"]
