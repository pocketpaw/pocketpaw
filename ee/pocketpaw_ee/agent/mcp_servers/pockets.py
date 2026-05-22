"""In-process SDK MCP server exposing cloud pocket context to agent backends.

Why MCP: the Claude Agent SDK passes the system prompt to ``claude.exe`` as a
CLI argument. Windows ``CreateProcess`` caps command lines at ~32KB (WinError
206), so embedding a full pocket document — including a large ``rippleSpec.ui``
tree — in the prompt is unsafe. The agent instead fetches the pocket on demand;
the response flows through the SDK's tool-result channel (stdio JSON, unbounded)
and never touches the CLI command line.

Pocket *designs* are still mutated through the ``pocket_specialist__create`` /
``pocket_specialist__edit`` MCP tools (see
``pocketpaw_ee.agent.pocket_specialist.mcp_tool``) — those rewrite a pocket's
``rippleSpec.ui``. This server now ALSO carries a writable ``add_widget`` tool:
it appends a single tile to a pocket's ``widgets[]`` array, which is the
surface the home grid renders. ``add_widget`` exists so the home-pocket agent
(on the ``claude_agent_sdk`` backend) can pin a real widget — a chart, a
table — without delegating to the design specialist.

Moved here from ``src/pocketpaw/agents/sdk_mcp_pocket.py`` in the OSS-EE split
(Phase 3b). That file also carried the ripple widget-spec tools, which have no
cloud dependency and stayed in core as ``pocketpaw.agents.sdk_mcp_widgets``.

Changes: 2026-05-22 (#1174) — added the writable ``add_widget`` tool, its
``ADD_WIDGET_TOOL_ID`` allowlist id, manifest validation of the widget's
rippleSpec ``spec`` subtree (skipped for ``type="native"`` widgets), and the
``_get_manifest_for_validation`` seam tests patch.
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
ADD_WIDGET_TOOL_ID = f"mcp__{SERVER_NAME}__add_widget"

POCKET_TOOL_IDS = (
    GET_POCKET_TOOL_ID,
    LIST_POCKETS_TOOL_ID,
    ADD_WIDGET_TOOL_ID,
)

# Widget ``type`` whose tiles carry no rippleSpec — the frontend renders them
# as a built-in Svelte component keyed on ``name``. Manifest validation, which
# only walks rippleSpec trees, is skipped for these.
_NATIVE_WIDGET_TYPE = "native"


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


def _error(text: str) -> dict:
    """Build an MCP error response. The agent reads ``text`` and retries."""
    return {"content": [{"type": "text", "text": f"Error: {text}"}], "is_error": True}


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


async def _get_manifest_for_validation() -> dict[str, Any] | None:
    """Fetch the Ripple widget manifest used to validate a widget's ``spec``.

    Best-effort: returns ``None`` on any failure so a manifest outage never
    blocks a widget add. A standalone seam so tests can patch it.
    """
    try:
        from pocketpaw.config import get_settings
        from pocketpaw.ripple.manifest import get_manifest

        settings = get_settings()
        return await get_manifest(
            settings.ripple_manifest_url,
            ttl_seconds=settings.ripple_manifest_ttl_seconds,
        )
    except Exception:  # noqa: BLE001
        logger.debug("ripple manifest fetch failed (non-fatal)", exc_info=True)
        return None


def _format_manifest_issues(issues: list[dict[str, Any]]) -> str:
    """Render ``validate_against_manifest`` issues as an agent-readable error
    string naming each offending prop so the agent can re-emit a fixed spec."""
    lines: list[str] = []
    for issue in issues[:10]:
        wtype = issue.get("type", "?")
        path = issue.get("path", "?")
        unknown = ", ".join(f"`{p}`" for p in issue.get("unknown_props", []))
        allowed = ", ".join(f"`{p}`" for p in issue.get("allowed_props", []))
        if unknown:
            lines.append(f"{path} ({wtype}): unknown props {unknown}; allowed: {allowed}")
    return "; ".join(lines)


async def _validate_widget_spec(widget: dict) -> str | None:
    """Validate the widget's rippleSpec ``spec`` subtree against the manifest.

    Returns an error string when the spec uses props the renderer would
    ignore, or ``None`` when the spec is clean / has no spec to check.
    ``type="native"`` widgets carry no rippleSpec — validation is skipped.
    Best-effort: a manifest outage returns ``None`` (never block the agent).
    """
    if widget.get("type") == _NATIVE_WIDGET_TYPE:
        return None
    spec = widget.get("spec")
    if not isinstance(spec, dict):
        return None
    manifest = await _get_manifest_for_validation()
    if manifest is None:
        return None
    from pocketpaw.ripple.manifest import validate_against_manifest

    issues = validate_against_manifest(spec, manifest, apply_aliases=True)
    actionable = [i for i in issues if i.get("unknown_props")]
    if not actionable:
        return None
    return (
        "The widget spec uses props the renderer ignores. "
        f"{_format_manifest_issues(actionable)}. "
        "Re-emit the widget using only the allowed props for each type."
    )


async def _add_widget_handler(args: dict) -> dict:
    """Append one widget tile to a pocket's ``widgets[]`` array.

    Validates the widget's rippleSpec ``spec`` against the renderer's
    manifest first (skipped for native widgets); on a validation failure
    nothing is persisted and the agent gets a corrective error.
    """
    pocket_id = args.get("pocket_id")
    if not pocket_id or not isinstance(pocket_id, str):
        return _error(
            "add_widget requires a `pocket_id` — pass the id of the pocket "
            "(the current home pocket) the widget should be pinned to."
        )
    widget = args.get("widget")
    if not isinstance(widget, dict):
        return _error("add_widget requires a `widget` object.")

    validation_error = await _validate_widget_spec(widget)
    if validation_error is not None:
        return _error(validation_error)

    from pocketpaw_ee.cloud.pockets.agent_context import add_widget_for_agent

    return _result_payload(await add_widget_for_agent(pocket_id, widget))


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

    @tool(
        "add_widget",
        (
            "Pin one widget tile onto a pocket's home grid. Use this on the "
            "home page to add a chart, table, list, stat, kanban, or any "
            "other Ripple-catalog widget the user asked for. Args: "
            "`pocket_id` (the pocket to pin onto — the current home pocket) "
            "and `widget`, an object with `name`, `type` (the Ripple "
            "catalog widget type, e.g. `chart`/`table`/`stat`/`list`/"
            "`kanban`), optional `icon`/`color`, and `spec` — a Ripple "
            "rippleSpec subtree for the tile (e.g. a `chart` node with a "
            "real `data` series). Call `get_widget_spec` for the widget "
            "type FIRST so the spec uses valid props; a spec with invented "
            'props is rejected. Native widgets pass `type:"native"` and '
            "no `spec`."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "description": "Id of the pocket to pin the widget onto.",
                },
                "widget": {
                    "type": "object",
                    "description": (
                        "Widget entry: name, type, optional icon/color, and a "
                        "rippleSpec `spec` subtree (omit for native widgets)."
                    ),
                },
            },
            "required": ["pocket_id", "widget"],
        },
    )
    async def add_widget(args):  # type: ignore[no-untyped-def]
        return await _add_widget_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.1.0",
        tools=[get_pocket, list_pockets, add_widget],
    )
    return SERVER_NAME, server


__all__ = [
    "ADD_WIDGET_TOOL_ID",
    "GET_POCKET_TOOL_ID",
    "LIST_POCKETS_TOOL_ID",
    "POCKET_TOOL_IDS",
    "SERVER_NAME",
    "build_pocket_context_server",
]
