"""Composio integration — MCP-direct tool provider for the parent chat agent.

Composio (https://docs.composio.dev) exposes 200+ pre-built OAuth-managed
integrations (Gmail, Slack, GitHub, Drive, Calendar, Linear, …) as a single
MCP server. This module wires the Composio MCP server into the parent
``claude_agent_sdk`` backend so the cloud chat agent can discover and call
any whitelisted toolkit at runtime, without us building per-toolkit Python
glue or owning each service's OAuth dance.

Architecture (v1, parent-agent only):
    settings ─┐
              ├─→ composio.service.get_session(ctx) ─→ Composio session
    ctx ──────┘                                              │
                                                             ▼
                                            composio.mcp.build_composio_mcp_server()
                                                             │
                                                             ▼
        src/pocketpaw/agents/claude_sdk.py::_get_mcp_servers (parent agent)

The pocket specialist sub-agent does NOT receive Composio MCP — when a
pocket UI needs Composio-sourced data, the parent fetches it first and
passes it into the specialist brief.
"""

from __future__ import annotations
