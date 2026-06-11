# instinct.py — in-process MCP server exposing read-only Instinct gate
# visibility to the claude_agent_sdk cloud chat backend. Created: 2026-06-11
# (feat/fabric-instinct-mcp-providers).
#
# Why this exists: on the claude_agent_sdk backend, PocketPaw registry tools
# (BaseTool) never reach the agent — only MCP servers do — and there was no
# instinct MCP provider, so the cloud chat agent had ZERO path to the Instinct
# gate (a live deployment had to ship its own stdio workaround). This module
# makes gate VISIBILITY first-class on that backend.
#
# It clones the external_actions.py / belt.py shape — a single
# ``create_sdk_mcp_server`` with an SDK import-guard, ``SERVER_NAME`` /
# ``*_TOOL_ID`` allowlist constants, ContextVar-sourced identity (the same
# ``current_workspace_id`` / ``current_user_id`` accessors in
# ``ee.cloud.chat.agent_service`` the sibling servers read), and the
# ``_error_response`` / ``_success_response`` helpers. The tool ids namespace
# as ``mcp__pocketpaw_instinct__<tool>`` so the Claude Code allowlist machinery
# matches them.
#
# Two SDK @tool defs, wrapping the existing registry-tool logic
# (``pocketpaw.tools.builtin.instinct_tools``):
#   * instinct_pending — list actions awaiting human approval, scoped to the
#     caller's workspace (W4a read filter). JSON-friendly, size-capped.
#   * instinct_audit — query the decision audit log (approvals, rejections,
#     proposals), workspace-scoped, limit-capped. JSON-friendly, size-capped.
#
# READ-ONLY — gate visibility only. InstinctProposeTool is deliberately NOT
# wrapped: gated proposing on the SDK backend goes through the
# ``pocketpaw_external_actions`` server (``propose_external_action``), which
# files a structured, executor-backed ``_external_action`` blob instead of a
# free-text Action. Neither tool here approves, rejects, executes, or proposes
# anything.
#
# Security: query inputs are bound as SQL parameters by the instinct store,
# never interpolated. The workspace id comes from the session's ContextVars,
# never from the agent's args, so an agent cannot read another tenant's
# pending queue or audit trail. Result payloads are size-capped so a large
# queue can't blow the model context. Action ``parameters`` blobs (which can
# carry diffs / call params) are NOT serialized — only gate-surface fields.
#
# EE→OSS boundary: this module lives in pocketpaw_ee and imports only
# ``pocketpaw`` (OSS) symbols at call time; core never imports this package.
"""Agent-side MCP surface for read-only Instinct gate visibility."""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_instinct"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form — keep the ids stable.
INSTINCT_PENDING_TOOL_ID = f"mcp__{SERVER_NAME}__instinct_pending"
INSTINCT_AUDIT_TOOL_ID = f"mcp__{SERVER_NAME}__instinct_audit"

INSTINCT_TOOL_IDS = (INSTINCT_PENDING_TOOL_ID, INSTINCT_AUDIT_TOOL_ID)

# Result-size caps. ``MAX_AUDIT_LIMIT`` mirrors the registry tool's clamp
# (``min(limit, 50)``); ``MAX_RESULT_BYTES`` bounds the serialized JSON body so
# a deep queue / audit trail can't blow the model context — rows are dropped
# from the TAIL until the body fits, and the response says so (truncated=true).
MAX_AUDIT_LIMIT = 50
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


def _get_instinct_store() -> Any | None:
    """Resolve the instinct store, or None when Instinct isn't available — the
    same lazy-import guard the registry tools use."""
    try:
        from pocketpaw.stores import get_instinct_store

        return get_instinct_store()
    except ImportError:
        return None


def _serialize_action(action: Any) -> dict[str, Any]:
    """A JSON-friendly projection of a pending Action — gate-surface fields
    only. The ``parameters`` blob (which can carry diffs / call params) is
    deliberately NOT serialized."""
    return {
        "id": action.id,
        "title": action.title,
        "recommendation": action.recommendation,
        "priority": action.priority.value,
        "category": action.category.value,
        "status": action.status.value,
        "pocket_id": action.pocket_id,
        "assignee": action.assignee,
        "created_at": action.created_at,
    }


def _serialize_audit_entry(entry: Any) -> dict[str, Any]:
    """A JSON-friendly projection of an AuditEntry — the fields the registry
    tool's text rendering surfaces, structured."""
    return {
        "id": entry.id,
        "action_id": entry.action_id,
        "pocket_id": entry.pocket_id,
        "timestamp": entry.timestamp,
        "actor": entry.actor,
        "event": entry.event,
        "category": entry.category.value,
        "description": entry.description,
        "ai_recommendation": entry.ai_recommendation,
        "outcome": entry.outcome,
    }


def _cap_rows(rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bool]:
    """Drop rows from the tail until the serialized list fits under
    ``MAX_RESULT_BYTES``. Returns ``(kept, truncated)``."""
    kept = list(rows)
    truncated = False
    while kept and len(json.dumps(kept, default=str).encode("utf-8")) > MAX_RESULT_BYTES:
        kept.pop()
        truncated = True
    return kept, truncated


async def _instinct_pending_handler(args: dict) -> dict:
    """MCP handler for ``instinct__instinct_pending``.

    Resolves identity, then lists actions pending human approval scoped to the
    caller's workspace. Returns ``{count, returned, truncated, actions}``.
    Read-only — never approves, rejects, executes, or proposes anything.
    """
    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response(
            "instinct_pending requires workspace context (call from a cloud chat session)."
        )

    pocket_id = args.get("pocket_id")
    if pocket_id is not None and not isinstance(pocket_id, str):
        return _error_response("instinct_pending `pocket_id` must be a string.")

    store = _get_instinct_store()
    if store is None:
        return _error_response("Instinct is not available (enterprise feature).")

    try:
        pending = await store.pending(pocket_id=pocket_id, workspace_id=workspace_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("instinct_pending failed", exc_info=True)
        return _error_response(f"could not read pending actions: {exc}")

    actions = [_serialize_action(a) for a in pending]
    actions, truncated = _cap_rows(actions)

    return _success_response(
        {
            "count": len(pending),
            "returned": len(actions),
            "truncated": truncated,
            "actions": actions,
        }
    )


async def _instinct_audit_handler(args: dict) -> dict:
    """MCP handler for ``instinct__instinct_audit``.

    Resolves identity, then queries the decision audit log scoped to the
    caller's workspace. Returns ``{count, returned, truncated, entries}``.
    Read-only.
    """
    workspace_id, _user_id = _identity()
    if not workspace_id:
        return _error_response(
            "instinct_audit requires workspace context (call from a cloud chat session)."
        )

    pocket_id = args.get("pocket_id")
    limit = args.get("limit", 10)
    if pocket_id is not None and not isinstance(pocket_id, str):
        return _error_response("instinct_audit `pocket_id` must be a string.")
    if not isinstance(limit, int) or limit < 1:
        return _error_response("instinct_audit `limit` must be a positive integer.")

    store = _get_instinct_store()
    if store is None:
        return _error_response("Instinct is not available (enterprise feature).")

    try:
        entries = await store.query_audit(
            pocket_id=pocket_id,
            limit=min(limit, MAX_AUDIT_LIMIT),
            workspace_id=workspace_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("instinct_audit failed", exc_info=True)
        return _error_response(f"could not read the audit log: {exc}")

    rows = [_serialize_audit_entry(e) for e in entries]
    rows, truncated = _cap_rows(rows)

    return _success_response(
        {
            "count": len(entries),
            "returned": len(rows),
            "truncated": truncated,
            "entries": rows,
        }
    )


def build_instinct_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for read-only Instinct gate
    visibility, or return ``None`` if the Claude Agent SDK isn't installed.

    Matches the shape returned by ``build_belt_server`` (``(name, server)`` or
    ``None``) so the backend's MCP registration loop treats it identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_instinct MCP disabled")
        return None

    @tool(
        "instinct_pending",
        (
            "List actions pending HUMAN APPROVAL in the Instinct gate (The "
            "Tray), scoped to the current workspace. READ-ONLY — this does not "
            "approve, reject, execute, or propose anything; use it to tell the "
            "user what's waiting on their decision. To PROPOSE a gated external "
            "call, use propose_external_action instead. Args: `pocket_id` "
            "(optional filter). Returns {count, returned, truncated, actions:"
            "[{id, title, recommendation, priority, category, status, "
            "pocket_id, assignee, created_at}]}. An error means relay the "
            "reason."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "description": "Filter by pocket (optional).",
                },
            },
            "additionalProperties": False,
        },
    )
    async def instinct_pending(args):  # type: ignore[no-untyped-def]
        return await _instinct_pending_handler(args)

    @tool(
        "instinct_audit",
        (
            "Query the Instinct decision audit log — recent proposals, "
            "approvals, rejections, and system events — scoped to the current "
            "workspace. READ-ONLY; useful for compliance questions and 'what "
            "happened' summaries. Args: `pocket_id` (optional filter), `limit` "
            "(max entries, default 10, cap 50). Returns {count, returned, "
            "truncated, entries:[{id, action_id, pocket_id, timestamp, actor, "
            "event, category, description, ai_recommendation, outcome}]}. An "
            "error means relay the reason."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "description": "Filter by pocket (optional).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries (default 10, cap 50).",
                },
            },
            "additionalProperties": False,
        },
    )
    async def instinct_audit(args):  # type: ignore[no-untyped-def]
        return await _instinct_audit_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[instinct_pending, instinct_audit],
    )
    return SERVER_NAME, server


__all__ = [
    "INSTINCT_AUDIT_TOOL_ID",
    "INSTINCT_PENDING_TOOL_ID",
    "INSTINCT_TOOL_IDS",
    "SERVER_NAME",
    "build_instinct_server",
]
