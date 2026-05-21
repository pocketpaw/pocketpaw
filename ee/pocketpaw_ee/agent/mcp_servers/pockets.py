"""In-process SDK MCP server exposing cloud pocket context to agent backends.

Why MCP: the Claude Agent SDK passes the system prompt to ``claude.exe`` as a
CLI argument. Windows ``CreateProcess`` caps command lines at ~32KB (WinError
206), so embedding a full pocket document — including a large ``rippleSpec.ui``
tree — in the prompt is unsafe. The agent instead fetches the pocket on demand;
the response flows through the SDK's tool-result channel (stdio JSON, unbounded)
and never touches the CLI command line.

Mutations DO NOT live on this server. Pocket mutations flow through the
``pocket_specialist__create`` / ``pocket_specialist__edit`` MCP tools (see
``pocketpaw_ee.agent.pocket_specialist.mcp_tool``). This module is a thin
adapter — the actual fetch lives in ``pocketpaw_ee.cloud.pockets.agent_context``.

Moved here from ``src/pocketpaw/agents/sdk_mcp_pocket.py`` in the OSS-EE split
(Phase 3b). That file also carried the ripple widget-spec tools, which have no
cloud dependency and stayed in core as ``pocketpaw.agents.sdk_mcp_widgets``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_pocket"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
GET_POCKET_TOOL_ID = f"mcp__{SERVER_NAME}__get_pocket"
LIST_POCKETS_TOOL_ID = f"mcp__{SERVER_NAME}__list_pockets"

POCKET_TOOL_IDS = (
    GET_POCKET_TOOL_ID,
    LIST_POCKETS_TOOL_ID,
)


def _result_payload(result: dict) -> dict:
    """Translate an ``agent_context`` ``{ok, pocket|error}`` dict into the MCP
    response shape."""
    if not result.get("ok"):
        return {
            "content": [{"type": "text", "text": f"Error: {result.get('error')}"}],
            "is_error": True,
        }
    body = result.get("pocket", result)
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":")),
            }
        ]
    }


async def _get_pocket_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.pockets.agent_context import fetch_pocket_for_agent

    return _result_payload(await fetch_pocket_for_agent(args.get("pocket_id", "")))


async def _list_pockets_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud.pockets.agent_context import list_pockets_for_agent

    result = await list_pockets_for_agent()
    if not result.get("ok"):
        return {
            "content": [{"type": "text", "text": f"Error: {result.get('error')}"}],
            "is_error": True,
        }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps({"pockets": result.get("pockets", [])}, separators=(",", ":")),
            }
        ]
    }


def build_pocket_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server, or None if the SDK is unavailable."""
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocket_context MCP disabled")
        return None

    @tool(
        "get_pocket",
        (
            "Fetch the full PocketPaw pocket document (rippleSpec, widgets, "
            "visibility, metadata) by id. Call this before answering any "
            "question about what a pocket contains, its widgets, or its layout."
        ),
        {"pocket_id": str},
    )
    async def get_pocket(args):  # type: ignore[no-untyped-def]
        return await _get_pocket_handler(args)

    @tool(
        "list_pockets",
        (
            "List every pocket in the user's workspace they can access "
            "(owned, shared, or workspace-visible). Returns id + name + "
            "description + type + icon + color per pocket — no rippleSpec, "
            "so the call is cheap. No arguments — workspace identity is "
            "inferred from the active stream."
        ),
        {},
    )
    async def list_pockets(args):  # type: ignore[no-untyped-def]
        return await _list_pockets_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[get_pocket, list_pockets],
    )
    return SERVER_NAME, server


__all__ = [
    "GET_POCKET_TOOL_ID",
    "LIST_POCKETS_TOOL_ID",
    "POCKET_TOOL_IDS",
    "SERVER_NAME",
    "build_pocket_context_server",
]
