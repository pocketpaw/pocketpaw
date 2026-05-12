"""In-process SDK MCP binding that exposes pocket context to the Claude Agent SDK.

Why MCP: the Claude Agent SDK passes the system prompt to ``claude.exe`` as a
CLI argument. Windows ``CreateProcess`` caps command lines at ~32KB (WinError
206), so embedding a full pocket document — including a large
``rippleSpec.ui`` tree — in the prompt is unsafe.

Instead we register an in-process MCP server with read-only pocket tools. The
agent fetches the full pocket on demand; the response flows through the SDK's
tool-result channel (stdio JSON, unbounded) and never touches the CLI command
line.

Mutations DO NOT live on this server. Pocket mutations flow through the
``pocket_specialist__create`` / ``pocket_specialist__edit`` MCP tools
(``ee/agent/pocket_specialist/mcp_tool.py``), which run an isolated specialist
backend with LangChain ``StructuredTool`` wrappers around the same
``*_for_agent`` functions. See ``ee/agent/pocket_specialist/tools.py``.

This module is a thin adapter — the actual fetch lives in
``ee/cloud/pockets/agent_context.py``.
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
GET_WIDGET_SPEC_TOOL_ID = f"mcp__{SERVER_NAME}__get_widget_spec"
GET_INLINE_WIDGET_HELP_TOOL_ID = f"mcp__{SERVER_NAME}__get_inline_widget_help"

POCKET_TOOL_IDS = (
    GET_POCKET_TOOL_ID,
    LIST_POCKETS_TOOL_ID,
    GET_WIDGET_SPEC_TOOL_ID,
    GET_INLINE_WIDGET_HELP_TOOL_ID,
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
    from ee.cloud.pockets.agent_context import fetch_pocket_for_agent

    return _result_payload(await fetch_pocket_for_agent(args.get("pocket_id", "")))


async def _list_pockets_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import list_pockets_for_agent

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


async def _get_widget_spec_handler(args: dict) -> dict:
    """Fetch the manifest, filter to requested widget types, and return a
    formatted markdown reference. Backs the ``get_widget_spec`` MCP tool."""
    from ee.ripple.manifest import format_for_prompt, get_manifest
    from pocketpaw.config import get_settings

    raw_types = args.get("types") or []
    if isinstance(raw_types, str):
        raw_types = [raw_types]
    requested = [t for t in raw_types if isinstance(t, str) and t]
    if not requested:
        return {
            "content": [
                {
                    "type": "text",
                    "text": "Error: pass `types` as a non-empty array of widget type names.",
                }
            ],
            "is_error": True,
        }

    settings = get_settings()
    manifest = await get_manifest(
        settings.ripple_manifest_url,
        ttl_seconds=settings.ripple_manifest_ttl_seconds,
    )
    if manifest is None:
        return {
            "content": [{"type": "text", "text": "Error: ripple manifest unavailable."}],
            "is_error": True,
        }

    widgets = manifest.get("widgets") or []
    by_type = {w.get("type"): w for w in widgets if w.get("type")}
    matched = [by_type[t] for t in requested if t in by_type]
    missing = [t for t in requested if t not in by_type]

    if not matched:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"No matching widgets. Unknown types: {', '.join(missing)}",
                }
            ],
            "is_error": True,
        }

    block = format_for_prompt({"widgets": matched})
    if missing:
        block += f"\n\n_Note: unknown types skipped: {', '.join(missing)}_"

    return {"content": [{"type": "text", "text": block}]}


async def _get_inline_widget_help_handler(args: dict) -> dict:
    """Handler for get_inline_widget_help — returns the slice of the
    chat-inline widget catalog matching the requested types.

    Args:
      types: list of widget kinds the agent intends to use
             (e.g. ["chart", "sparkline"]). Empty / missing → full
             catalog (rare — agent generally knows what it wants).
    """
    from ee.ripple._inline_core import widget_help

    types = args.get("types") or []
    if not isinstance(types, list):
        types = []
    return {"content": [{"type": "text", "text": widget_help([str(t) for t in types])}]}


def build_pocket_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server, or return None if the SDK is unavailable."""
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

    @tool(
        "get_widget_spec",
        (
            "Get full props, types, and example ui-spec for one or more "
            "Ripple widgets. Pass ``types`` as an array of widget type names "
            "(e.g. ``['feed', 'timeline', 'stat']``). Returns a markdown "
            "reference with each widget's props schema and a runnable example. "
            "MANDATORY before composing a ui-spec for any widget not in the "
            "FREE LIST under WIDGET SPEC TOOL RULE — never guess prop names "
            "or shapes from the widget name. Batch types in a single call. "
            "Available types are listed under WIDGET CATALOG in the system "
            "prompt."
        ),
        {"types": list},
    )
    async def get_widget_spec(args):  # type: ignore[no-untyped-def]
        return await _get_widget_spec_handler(args)

    @tool(
        "get_inline_widget_help",
        "Return the chat-inline Ripple widget catalog. Call this BEFORE "
        "emitting any non-core widget in a ui-spec fence (anything "
        "beyond text/heading/stat/button/table/flex). Pass the widget "
        "types you intend to use; you receive the canonical prop "
        "schema for those widgets so the spec renders on the first "
        "try. Cheap, in-process, single round-trip.",
        {
            "type": "object",
            "properties": {
                "types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Widget kinds you plan to use, e.g. "
                        "['chart', 'sparkline']. Empty returns the "
                        "full catalog."
                    ),
                }
            },
        },
    )
    async def get_inline_widget_help(args):  # type: ignore[no-untyped-def]
        return await _get_inline_widget_help_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[
            get_pocket,
            list_pockets,
            get_widget_spec,
            get_inline_widget_help,
        ],
    )
    return SERVER_NAME, server
