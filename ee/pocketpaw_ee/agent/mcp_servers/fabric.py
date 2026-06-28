# fabric.py — in-process MCP server exposing read-only Fabric ontology access
# to the claude_agent_sdk cloud chat backend. Created: 2026-06-11
# (feat/fabric-instinct-mcp-providers).
# Updated: 2026-06-11 (fix/fabric-stats-workspace-scope) — fabric_stats now
# passes the resolved workspace into the store's scoped stats()/list_types(),
# closing the live cross-tenant type-name leak the original instance-wide
# stats had on a shared fabric.db.
#
# Why this exists: on the claude_agent_sdk backend, PocketPaw registry tools
# (BaseTool) never reach the agent — only MCP servers do — and there was no
# fabric MCP provider, so the cloud chat agent had ZERO path to the Fabric
# ontology (a live deployment had to ship its own stdio workaround). This
# module makes Fabric first-class on that backend.
#
# It clones the external_actions.py / belt.py shape — a single
# ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, ContextVar-sourced identity (the same
# ``current_workspace_id`` / ``current_user_id`` accessors in
# ``ee.cloud.chat.agent_service`` the sibling servers read), and the
# ``_error_response`` / ``_success_response`` helpers. The tool ids namespace
# as ``mcp__pocketpaw_fabric__<tool>`` so the Claude Code allowlist machinery
# matches them.
#
# Two SDK @tool defs, wrapping the existing registry-tool logic
# (``pocketpaw.tools.builtin.fabric_tools``) — the tool NAMES are pinned
# (``fabric_query`` / ``fabric_stats``): a deployed skill already calls them.
#   * fabric_query — runs a FabricQuery against the fabric store, scoped to the
#     caller's workspace (W4a read filter: the tenant's rows plus legacy
#     NULL-workspace rows). Unlike the BaseTool (formatted text), results are
#     returned JSON-friendly ({total, returned, truncated, objects}) and
#     size-capped: the limit clamps to MAX_QUERY_LIMIT and oversized result
#     sets are truncated from the tail under MAX_RESULT_BYTES.
#   * fabric_stats — ontology counts + type names, scoped to the caller's
#     workspace (fix/fabric-stats-workspace-scope: the original instance-wide
#     stats leaked another tenant's experimental type names into chat on a
#     shared box). Counts mirror fabric_query's visibility exactly; the type
#     list holds only the caller workspace's own types (SZD-2: the
#     fabric_object_types table now carries a workspace_id; a NULL workspace_id
#     is a legacy/global type visible to all — see FabricStore.list_types).
#
# Read-only: neither tool writes anything. FabricCreateTool is deliberately
# NOT wrapped — ontology writes from the SDK backend should arrive as gated
# proposals, not ambient writes.
#
# Security: query inputs are DATA — they are bound as SQL parameters by the
# fabric store, never interpolated. The workspace id comes from the session's
# ContextVars, never from the agent's args, so an agent cannot query another
# tenant's rows. Result payloads are size-capped so a huge ontology can't blow
# the model context.
#
# EE→OSS boundary: this module lives in pocketpaw_ee and imports only
# ``pocketpaw`` (OSS) symbols at call time; core never imports this package.
"""Agent-side MCP surface for read-only Fabric ontology access."""

from __future__ import annotations

import json
import logging
from typing import Any

from ._audit import record_tool_call

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_fabric"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form. A deployed skill already calls
# ``fabric_query`` / ``fabric_stats`` — keep the tool names stable.
FABRIC_QUERY_TOOL_ID = f"mcp__{SERVER_NAME}__fabric_query"
FABRIC_STATS_TOOL_ID = f"mcp__{SERVER_NAME}__fabric_stats"

FABRIC_TOOL_IDS = (FABRIC_QUERY_TOOL_ID, FABRIC_STATS_TOOL_ID)

# Result-size caps. ``MAX_QUERY_LIMIT`` mirrors the registry tool's clamp
# (``min(limit, 50)``); ``MAX_RESULT_BYTES`` bounds the serialized JSON body so
# a wide ontology row set can't blow the model context — objects are dropped
# from the TAIL until the body fits, and the response says so (truncated=true).
MAX_QUERY_LIMIT = 50
MAX_RESULT_BYTES = 48 * 1024


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects. The agent
    reads ``text`` and surfaces the reason."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _identity() -> tuple[str | None, str | None]:
    """Resolve the active workspace + user from the per-stream ContextVars set
    by the cloud chat agent runtime. Returns ``(workspace_id, user_id)``."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import (
            current_user_id,
            current_workspace_id,
        )

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001
        return None, None


def _get_fabric_store() -> Any | None:
    """Resolve the fabric store, or None when Fabric isn't available — the
    same lazy-import guard the registry tools use."""
    try:
        from pocketpaw.stores import get_fabric_store

        return get_fabric_store()
    except ImportError:
        return None


def _serialize_object(obj: Any) -> dict[str, Any]:
    """A JSON-friendly projection of a FabricObject — the fields the registry
    tool's text rendering surfaces, structured."""
    return {
        "id": obj.id,
        "type_name": obj.type_name,
        "properties": dict(obj.properties or {}),
        "source_connector": obj.source_connector,
        "source_id": obj.source_id,
    }


def _cap_objects(objects: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Drop objects from the tail until the serialized list fits under
    ``MAX_RESULT_BYTES``. Returns ``(kept, truncated)``."""
    kept = list(objects)
    truncated = False
    while kept and len(json.dumps(kept, default=str).encode("utf-8")) > MAX_RESULT_BYTES:
        kept.pop()
        truncated = True
    return kept, truncated


async def _fabric_query_handler(args: dict) -> dict:
    """MCP handler for ``fabric__fabric_query``.

    Resolves identity, validates inputs, runs the FabricQuery scoped to the
    caller's workspace, and returns ``{total, returned, truncated, objects}``.
    Read-only — never writes. Errors return a plain relayable message.
    """
    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response(
            "fabric_query requires workspace context (call from a cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=_user_id,
        tool_server="pocketpaw_fabric",
        tool_name="_fabric_query",
        status="ok",
        ok=True,
    )

    type_name = args.get("type_name")
    linked_to = args.get("linked_to")
    link_type = args.get("link_type")
    filters = args.get("filters")
    limit = args.get("limit", 20)

    for field_name, value in (
        ("type_name", type_name),
        ("linked_to", linked_to),
        ("link_type", link_type),
    ):
        if value is not None and not isinstance(value, str):
            return _error_response(f"fabric_query `{field_name}` must be a string.")
    if filters is not None and not isinstance(filters, dict):
        return _error_response(
            "fabric_query `filters` must be a JSON object mapping property names "
            "to a scalar (equality) or an operator map (comparison)."
        )
    if not isinstance(limit, int) or limit < 1:
        return _error_response("fabric_query `limit` must be a positive integer.")

    store = _get_fabric_store()
    if store is None:
        return _error_response("Fabric is not available (enterprise feature).")

    try:
        from pocketpaw.fabric.models import FabricQuery

        result = await store.query(
            FabricQuery(
                type_name=type_name,
                linked_to=linked_to,
                link_type=link_type,
                filters=filters or {},
                limit=min(limit, MAX_QUERY_LIMIT),
            ),
            workspace_id=workspace_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("fabric_query failed (type=%s)", type_name, exc_info=True)
        return _error_response(f"could not query Fabric: {exc}")

    # Best-effort trace emission — same telemetry the registry tool publishes
    # (a no-op unless a proposal trace is actively collecting). Never fails the
    # query response.
    try:
        from pocketpaw.tools.builtin.fabric_tools import _emit_trace_events

        await _emit_trace_events(
            "fabric_query",
            [{"object_id": o.id, "object_type": o.type_name} for o in result.objects],
        )
    except Exception:  # noqa: BLE001 — telemetry must never break the read
        logger.debug("fabric_query trace emission skipped", exc_info=True)

    objects = [_serialize_object(o) for o in result.objects]
    objects, truncated = _cap_objects(objects)

    return _success_response(
        {
            "total": result.total,
            "returned": len(objects),
            "truncated": truncated,
            "objects": objects,
        }
    )


async def _fabric_stats_handler(args: dict) -> dict:
    """MCP handler for ``fabric__fabric_stats``.

    Returns ontology counts + type names: ``{types, objects, links,
    type_names}``, scoped to the caller's workspace so stats and fabric_query
    agree (own rows plus legacy NULL-workspace rows). The type list holds only
    types with object rows visible to the workspace — never another tenant's
    experiment names. Read-only.
    """
    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response(
            "fabric_stats requires workspace context (call from a cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=_user_id,
        tool_server="pocketpaw_fabric",
        tool_name="_fabric_stats",
        status="ok",
        ok=True,
    )

    store = _get_fabric_store()
    if store is None:
        return _error_response("Fabric is not available (enterprise feature).")

    try:
        stats = await store.stats(workspace_id=workspace_id)
        types = await store.list_types(workspace_id=workspace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("fabric_stats failed", exc_info=True)
        return _error_response(f"could not read Fabric stats: {exc}")

    return _success_response(
        {
            "types": stats.get("types", 0),
            "objects": stats.get("objects", 0),
            "links": stats.get("links", 0),
            "type_names": [t.name for t in types],
        }
    )


def build_fabric_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for read-only Fabric access, or
    return ``None`` if the Claude Agent SDK isn't installed.

    Matches the shape returned by ``build_belt_server`` (``(name, server)`` or
    ``None``) so the backend's MCP registration loop treats it identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_fabric MCP disabled")
        return None

    @tool(
        "fabric_query",
        (
            "Query the Fabric ontology to find business objects and their "
            "relationships. Search by object type (e.g. 'Customer', 'Order'), "
            "filter by property values, or traverse links between objects. "
            "READ-ONLY — never creates or modifies anything. Results are scoped "
            "to the current workspace. Args: `type_name` (object type to "
            "search), `linked_to` (find objects linked to this object id), "
            "`link_type` (filter links by type), `filters` (property filters — "
            'a scalar for equality or an operator map like {"rent": {">": '
            "1000}}), `limit` (max results, default 20, cap 50). Returns "
            "{total, returned, truncated, objects:[{id, type_name, properties, "
            "source_connector, source_id}]}. An error means relay the reason."
        ),
        {
            "type": "object",
            "properties": {
                "type_name": {
                    "type": "string",
                    "description": "Object type to search (e.g. 'Customer', 'Order').",
                },
                "linked_to": {
                    "type": "string",
                    "description": "Find objects linked to this object ID.",
                },
                "link_type": {
                    "type": "string",
                    "description": "Filter links by type (e.g. 'has_order', 'belongs_to').",
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Filter objects by property values. Scalar = equality; "
                        "operator map = comparison (=, !=, >, >=, <, <=)."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 20, cap 50).",
                },
            },
            "additionalProperties": False,
        },
    )
    async def fabric_query(args):  # type: ignore[no-untyped-def]
        return await _fabric_query_handler(args)

    @tool(
        "fabric_stats",
        (
            "Get statistics about the Fabric ontology: number of object types, "
            "objects, and links, plus the list of type names — scoped to the "
            "current workspace, consistent with fabric_query. READ-ONLY. Takes "
            "no arguments. Returns {types, objects, links, type_names}. An "
            "error means relay the reason."
        ),
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    async def fabric_stats(args):  # type: ignore[no-untyped-def]
        return await _fabric_stats_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[fabric_query, fabric_stats],
    )
    return SERVER_NAME, server


__all__ = [
    "FABRIC_QUERY_TOOL_ID",
    "FABRIC_STATS_TOOL_ID",
    "FABRIC_TOOL_IDS",
    "SERVER_NAME",
    "build_fabric_server",
]
