"""MCP server (thin: tool handlers delegate to runtime / skills)."""

from agentify.mcp.server import AgentifyApp, build_app, serve_stdio, tool_name_for

__all__ = ["AgentifyApp", "build_app", "serve_stdio", "tool_name_for"]
