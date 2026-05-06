"""In-process SDK MCP binding that exposes pocket context to the Claude Agent SDK.

Why MCP: the Claude Agent SDK passes the system prompt to ``claude.exe`` as a
CLI argument. Windows ``CreateProcess`` caps command lines at ~32KB (WinError
206), so embedding a full pocket document — including a large
``rippleSpec.ui`` tree — in the prompt is unsafe.

Instead we register an in-process MCP server with a ``get_pocket`` tool. The
agent fetches the full pocket on demand; the response flows through the SDK's
tool-result channel (stdio JSON, unbounded) and never touches the CLI command
line.

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
CREATE_POCKET_TOOL_ID = f"mcp__{SERVER_NAME}__create_pocket"
UPDATE_POCKET_TOOL_ID = f"mcp__{SERVER_NAME}__update_pocket"
ADD_WIDGET_TOOL_ID = f"mcp__{SERVER_NAME}__add_widget"
UPDATE_WIDGET_TOOL_ID = f"mcp__{SERVER_NAME}__update_widget"
REMOVE_WIDGET_TOOL_ID = f"mcp__{SERVER_NAME}__remove_widget"
GET_WIDGET_SPEC_TOOL_ID = f"mcp__{SERVER_NAME}__get_widget_spec"

POCKET_TOOL_IDS = (
    GET_POCKET_TOOL_ID,
    LIST_POCKETS_TOOL_ID,
    CREATE_POCKET_TOOL_ID,
    UPDATE_POCKET_TOOL_ID,
    ADD_WIDGET_TOOL_ID,
    UPDATE_WIDGET_TOOL_ID,
    REMOVE_WIDGET_TOOL_ID,
    GET_WIDGET_SPEC_TOOL_ID,
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
                "text": json.dumps(
                    {"pockets": result.get("pockets", [])}, separators=(",", ":")
                ),
            }
        ]
    }


async def _update_pocket_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import update_pocket_for_agent

    return _result_payload(
        await update_pocket_for_agent(
            args.get("pocket_id", ""),
            name=args.get("name"),
            description=args.get("description"),
            icon=args.get("icon"),
            color=args.get("color"),
            ripple_spec=args.get("ripple_spec"),
        )
    )


async def _create_pocket_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import create_pocket_for_agent

    return _result_payload(
        await create_pocket_for_agent(
            name=args.get("name", ""),
            description=args.get("description", ""),
            type_=args.get("type", "custom"),
            icon=args.get("icon", ""),
            color=args.get("color", ""),
            ripple_spec=args.get("ripple_spec"),
        )
    )


async def _add_widget_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import add_widget_for_agent

    return _result_payload(
        await add_widget_for_agent(args.get("pocket_id", ""), args.get("widget", {}))
    )


async def _update_widget_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import update_widget_for_agent

    return _result_payload(
        await update_widget_for_agent(
            args.get("pocket_id", ""),
            args.get("widget_id", ""),
            args.get("fields", {}),
        )
    )


async def _remove_widget_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import remove_widget_for_agent

    return _result_payload(
        await remove_widget_for_agent(args.get("pocket_id", ""), args.get("widget_id", ""))
    )


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
            "content": [
                {"type": "text", "text": "Error: ripple manifest unavailable."}
            ],
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
            "so the call is cheap. CALL THIS BEFORE ``create_pocket`` to "
            "see if a similar pocket already exists; the system prompt "
            "tells you to prefer extending an existing pocket over "
            "spawning a duplicate. No arguments — workspace identity is "
            "inferred from the active stream."
        ),
        {},
    )
    async def list_pockets(args):  # type: ignore[no-untyped-def]
        return await _list_pockets_handler(args)

    @tool(
        "create_pocket",
        (
            "Materialize a brand-new pocket (themed dashboard / canvas) "
            "for the user. Pass ``name`` (required), ``description``, "
            "``type`` (research|business|data|mission|deep-work|custom|"
            "hospitality), ``icon``, ``color``, and ``ripple_spec`` — a "
            "UISpec v1.0 component tree (root with ``type``/``props``/"
            "``children``). Persists the Pocket document and emits a "
            "``pocket_created`` SSE event so the user's canvas mounts the "
            "new pocket immediately. Use this — do NOT respond with an "
            "inline ``ui-spec`` block when the user asked you to BUILD a "
            "pocket; that only renders inside the chat bubble."
        ),
        {
            "name": str,
            "description": str,
            "type": str,
            "icon": str,
            "color": str,
            "ripple_spec": dict,
        },
    )
    async def create_pocket(args):  # type: ignore[no-untyped-def]
        return await _create_pocket_handler(args)

    @tool(
        "update_pocket",
        (
            "Patch top-level fields on a pocket. Pass ``ripple_spec`` to "
            "replace the rendered UI tree (UISpec v1.0 / UniversalSpec v2.0). "
            "Other patchable fields: name, description, icon, color. "
            "Omit a field to leave it unchanged. Returns the updated pocket "
            "document. Always call ``get_pocket`` first so you keep "
            "non-edited parts of the spec intact."
        ),
        {
            "pocket_id": str,
            "name": str,
            "description": str,
            "icon": str,
            "color": str,
            "ripple_spec": dict,
        },
    )
    async def update_pocket(args):  # type: ignore[no-untyped-def]
        return await _update_pocket_handler(args)

    @tool(
        "add_widget",
        (
            "Append a widget to a pocket's embedded widget list. ``widget`` is "
            "an object: {name, type, icon?, color?, span?, dataSourceType?, "
            "config?, props?, data?, assignedAgent?}. For ripple-rendered "
            "pockets, prefer ``update_pocket`` with a new ``ripple_spec`` "
            "instead — the embedded widget list is the legacy widgets-grid "
            "format."
        ),
        {"pocket_id": str, "widget": dict},
    )
    async def add_widget(args):  # type: ignore[no-untyped-def]
        return await _add_widget_handler(args)

    @tool(
        "update_widget",
        (
            "Patch fields on a single embedded widget. ``fields`` is a partial "
            "object — only present keys are written. Patchable: name, type, "
            "icon, color, span, dataSourceType, config, props, data, "
            "assignedAgent."
        ),
        {"pocket_id": str, "widget_id": str, "fields": dict},
    )
    async def update_widget(args):  # type: ignore[no-untyped-def]
        return await _update_widget_handler(args)

    @tool(
        "remove_widget",
        "Remove a widget from a pocket's embedded widget list by widget_id.",
        {"pocket_id": str, "widget_id": str},
    )
    async def remove_widget(args):  # type: ignore[no-untyped-def]
        return await _remove_widget_handler(args)

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

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[
            get_pocket,
            list_pockets,
            create_pocket,
            update_pocket,
            add_widget,
            update_widget,
            remove_widget,
            get_widget_spec,
        ],
    )
    return SERVER_NAME, server
