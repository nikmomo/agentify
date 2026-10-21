"""Deterministic in-memory adapter for unit tests. Never evidence of real application support."""

from agentify.adapters.fake.adapter import FakeAdapter, FakeApp, FakeDesktop, FakeNode, FakeWindow
from agentify.adapters.fake.scenarios import colour_fixture_app, simple_editor_app

__all__ = [
    "FakeAdapter",
    "FakeApp",
    "FakeDesktop",
    "FakeNode",
    "FakeWindow",
    "colour_fixture_app",
    "simple_editor_app",
]
