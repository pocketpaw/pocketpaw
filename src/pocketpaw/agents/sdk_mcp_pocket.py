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
GET_INLINE_WIDGET_HELP_TOOL_ID = f"mcp__{SERVER_NAME}__get_inline_widget_help"

# Granular rippleSpec.ui mutation tools — the preferred surface for
# surgical edits. Live alongside ``update_pocket`` (which stays for
# whole-canvas rewrites and initial creation).
ADD_NODE_TOOL_ID = f"mcp__{SERVER_NAME}__add_node"
REPLACE_NODE_TOOL_ID = f"mcp__{SERVER_NAME}__replace_node"
SET_NODE_PROP_TOOL_ID = f"mcp__{SERVER_NAME}__set_node_prop"
MOVE_NODE_TOOL_ID = f"mcp__{SERVER_NAME}__move_node"
REMOVE_NODE_TOOL_ID = f"mcp__{SERVER_NAME}__remove_node"

# Granular rippleSpec.state mutation tools — the "data" half of the
# mutation surface. Widgets bound to {state.x} re-render automatically
# when state changes, so set_state is the cheapest way to update what
# the user sees without touching widget structure.
SET_STATE_TOOL_ID = f"mcp__{SERVER_NAME}__set_state"
APPEND_STATE_TOOL_ID = f"mcp__{SERVER_NAME}__append_state"
REMOVE_STATE_TOOL_ID = f"mcp__{SERVER_NAME}__remove_state"
PATCH_STATE_TOOL_ID = f"mcp__{SERVER_NAME}__patch_state"

POCKET_TOOL_IDS = (
    GET_POCKET_TOOL_ID,
    LIST_POCKETS_TOOL_ID,
    CREATE_POCKET_TOOL_ID,
    UPDATE_POCKET_TOOL_ID,
    ADD_WIDGET_TOOL_ID,
    UPDATE_WIDGET_TOOL_ID,
    REMOVE_WIDGET_TOOL_ID,
    GET_WIDGET_SPEC_TOOL_ID,
    GET_INLINE_WIDGET_HELP_TOOL_ID,
    ADD_NODE_TOOL_ID,
    REPLACE_NODE_TOOL_ID,
    SET_NODE_PROP_TOOL_ID,
    MOVE_NODE_TOOL_ID,
    REMOVE_NODE_TOOL_ID,
    SET_STATE_TOOL_ID,
    APPEND_STATE_TOOL_ID,
    REMOVE_STATE_TOOL_ID,
    PATCH_STATE_TOOL_ID,
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


async def _add_node_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import add_node_for_agent

    return _result_payload(
        await add_node_for_agent(
            args.get("pocket_id", ""),
            args.get("parent_id", ""),
            args.get("spec") or {},
            after_id=args.get("after_id"),
        )
    )


async def _replace_node_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import replace_node_for_agent

    return _result_payload(
        await replace_node_for_agent(
            args.get("pocket_id", ""),
            args.get("node_id", ""),
            args.get("spec") or {},
        )
    )


async def _set_node_prop_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import set_node_prop_for_agent

    return _result_payload(
        await set_node_prop_for_agent(
            args.get("pocket_id", ""),
            args.get("node_id", ""),
            args.get("prop", ""),
            args.get("value"),
        )
    )


async def _move_node_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import move_node_for_agent

    return _result_payload(
        await move_node_for_agent(
            args.get("pocket_id", ""),
            args.get("node_id", ""),
            args.get("new_parent_id", ""),
            after_id=args.get("after_id"),
        )
    )


async def _remove_node_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import remove_node_for_agent

    return _result_payload(
        await remove_node_for_agent(
            args.get("pocket_id", ""),
            args.get("node_id", ""),
        )
    )


async def _set_state_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import set_state_for_agent

    return _result_payload(
        await set_state_for_agent(
            args.get("pocket_id", ""),
            args.get("path", ""),
            args.get("value"),
        )
    )


async def _append_state_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import append_state_for_agent

    return _result_payload(
        await append_state_for_agent(
            args.get("pocket_id", ""),
            args.get("path", ""),
            args.get("item"),
        )
    )


async def _remove_state_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import remove_state_for_agent

    return _result_payload(
        await remove_state_for_agent(
            args.get("pocket_id", ""),
            args.get("path", ""),
        )
    )


async def _patch_state_handler(args: dict) -> dict:
    from ee.cloud.pockets.agent_context import patch_state_for_agent

    return _result_payload(
        await patch_state_for_agent(
            args.get("pocket_id", ""),
            args.get("partial") or {},
        )
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
        "add_node",
        (
            "Insert a new node into the pocket's UI tree as a child of "
            "``parent_id``. Use this for SURGICAL adds (one chart, one "
            "row, one card) instead of rewriting the whole rippleSpec "
            "via ``update_pocket``. Pass ``spec`` as a UINode object "
            "(``{type, props?, children?, on_click?, ...}``) — an ``id`` "
            "is auto-assigned if you omit it. Use ``after_id`` to "
            "position the new node immediately after a specific sibling; "
            "omit it to append. Returns ``{ok, node_id, subtree}``. "
            "Errors loudly when ``parent_id`` is unknown or ``after_id`` "
            "isn't a child of the parent."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {"type": "string"},
                "parent_id": {
                    "type": "string",
                    "description": "id of the node to insert under",
                },
                "spec": {
                    "type": "object",
                    "description": "UINode to insert ({type, props?, children?, ...})",
                },
                "after_id": {
                    "type": "string",
                    "description": (
                        "Insert immediately after this sibling. Omit to "
                        "append to the end of parent's children."
                    ),
                },
            },
            "required": ["pocket_id", "parent_id", "spec"],
        },
    )
    async def add_node(args):  # type: ignore[no-untyped-def]
        return await _add_node_handler(args)

    @tool(
        "replace_node",
        (
            "Replace the subtree at ``node_id`` with ``spec``. The "
            "target's id is preserved if ``spec.id`` is absent — callers "
            "rarely need to set it. Use for shape-changing edits (swap "
            "a stat card for a chart). For prop-only tweaks, prefer "
            "``set_node_prop``. Errors when ``node_id`` is unknown or "
            "is the root (use ``update_pocket`` to rewrite the whole "
            "canvas)."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {"type": "string"},
                "node_id": {"type": "string"},
                "spec": {
                    "type": "object",
                    "description": "Replacement UINode",
                },
            },
            "required": ["pocket_id", "node_id", "spec"],
        },
    )
    async def replace_node(args):  # type: ignore[no-untyped-def]
        return await _replace_node_handler(args)

    @tool(
        "set_node_prop",
        (
            "Set a single prop on a node. CHEAPEST surgical edit — use "
            "this for label tweaks, value updates, toggling ``show``, "
            "rewiring an ``on_click``. ``prop`` writes into ``props`` by "
            'default (``prop="label"`` → ``node.props.label``). '
            "Top-level keys (``show``, ``bind``, ``class``, ``style``, "
            "``slot``, ``items``, ``condition``, and ``on_*`` handlers) "
            "are addressable by bare name. Dotted paths (``data.rows``) "
            "walk inside ``props``. ``value`` may be any JSON. Returns "
            "``{ok, subtree, old_value}``."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {"type": "string"},
                "node_id": {"type": "string"},
                "prop": {
                    "type": "string",
                    "description": "Prop name (or dotted path inside props)",
                },
                "value": {"description": "New value — any JSON-serialisable type"},
            },
            "required": ["pocket_id", "node_id", "prop", "value"],
        },
    )
    async def set_node_prop(args):  # type: ignore[no-untyped-def]
        return await _set_node_prop_handler(args)

    @tool(
        "move_node",
        (
            "Move a subtree under a new parent. Same op handles "
            "reorder-within-parent and cross-parent moves. ``after_id`` "
            "positions immediately after a target sibling at the new "
            "location; omit to append. Refuses to move a node into "
            "itself or a descendant. Returns ``{ok, subtree}``."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {"type": "string"},
                "node_id": {"type": "string"},
                "new_parent_id": {"type": "string"},
                "after_id": {"type": "string"},
            },
            "required": ["pocket_id", "node_id", "new_parent_id"],
        },
    )
    async def move_node(args):  # type: ignore[no-untyped-def]
        return await _move_node_handler(args)

    @tool(
        "remove_node",
        (
            "Remove the subtree rooted at ``node_id``. Errors on the "
            "root (the pocket itself is removed via the pocket-level "
            "delete API, not this tool). Returns ``{ok, removed_id}``."
        ),
        {"pocket_id": str, "node_id": str},
    )
    async def remove_node(args):  # type: ignore[no-untyped-def]
        return await _remove_node_handler(args)

    @tool(
        "set_state",
        (
            "Write a single value into the pocket's state at ``path``. "
            "CHEAPEST DATA EDIT — every widget bound to ``{state.<path>}`` "
            "re-renders automatically. Use this for filter changes, "
            "current-selection updates, toggling boolean flags, editing "
            "field values on a record — anything the user sees as "
            "DATA. ``path`` syntax: dotted with bracket indexing — "
            "``filter``, ``user.name``, ``tasks[0].status``, "
            "``groups[2].members[1].id``. ``value`` may be any JSON. "
            "Returns ``{ok, old_value}``."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {"type": "string"},
                "path": {
                    "type": "string",
                    "description": (
                        "Dotted path with optional bracket indices, e.g. tasks[0].status"
                    ),
                },
                "value": {"description": "New value — any JSON-serialisable type"},
            },
            "required": ["pocket_id", "path", "value"],
        },
    )
    async def set_state(args):  # type: ignore[no-untyped-def]
        return await _set_state_handler(args)

    @tool(
        "append_state",
        (
            "Append ``item`` to the array at ``path``. Creates an empty "
            "list at the path if absent. Use for adding tasks, log "
            "entries, comments, items to a kanban column. Returns "
            "``{ok, new_length}``."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {"type": "string"},
                "path": {"type": "string"},
                "item": {"description": "Element to append — any JSON type"},
            },
            "required": ["pocket_id", "path", "item"],
        },
    )
    async def append_state(args):  # type: ignore[no-untyped-def]
        return await _append_state_handler(args)

    @tool(
        "remove_state",
        (
            "Remove the value at ``path``. For dict keys, deletes the "
            "key. For list indices (``tasks[1]``), removes the element "
            "and shifts subsequent indices down. Returns "
            "``{ok, removed}`` (the removed value, used as the inverse "
            "for undo)."
        ),
        {"pocket_id": str, "path": str},
    )
    async def remove_state(args):  # type: ignore[no-untyped-def]
        return await _remove_state_handler(args)

    @tool(
        "patch_state",
        (
            "Shallow-merge ``partial`` into state's top level. For "
            "BATCHED writes when several independent keys change at "
            "once (e.g. resetting a form: ``{name: '', email: '', "
            "submitted: false}``). Note: shallow only — nested dicts "
            "get REPLACED, not merged. For nested updates, prefer "
            "multiple ``set_state`` calls. Returns ``{ok, previous}``."
        ),
        {"pocket_id": str, "partial": dict},
    )
    async def patch_state(args):  # type: ignore[no-untyped-def]
        return await _patch_state_handler(args)

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
            create_pocket,
            update_pocket,
            add_widget,
            update_widget,
            remove_widget,
            add_node,
            replace_node,
            set_node_prop,
            move_node,
            remove_node,
            set_state,
            append_state,
            remove_state,
            patch_state,
            get_widget_spec,
            get_inline_widget_help,
        ],
    )
    return SERVER_NAME, server
