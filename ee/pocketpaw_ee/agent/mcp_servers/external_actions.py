# external_actions.py — in-process MCP server exposing the gated external-action
# proposal type to the claude_agent_sdk cloud chat backend. Created: 2026-06-11
# (feat/external-action-mcp-tool).
#
# What this file does: it is the agent-facing surface for the THIRD gated
# Instinct proposal type — the external action (#1425 added the propose/execute
# halves under ee/cloud/external_actions/ but no agent-facing tool). It clones
# the belt.py shape — a single ``create_sdk_mcp_server`` with an SDK import-
# guard, ``SERVER_NAME`` / ``*_TOOL_ID`` allowlist constants, ContextVar-sourced
# identity (the same ``current_workspace_id`` / ``current_user_id`` /
# ``current_session_mongo_id`` accessors in ``ee.cloud.chat.agent_service`` the
# belt / media / sites servers read), and the ``_error_response`` /
# ``_success_response`` helpers. The single tool id namespaces as
# ``mcp__pocketpaw_external_actions__propose_external_action`` so the Claude
# Code allowlist machinery matches it.
#
# One SDK @tool def:
#   * propose_external_action — a chat agent proposes a call to an external
#     system through a bound connector ("call action ``approveApplication`` on
#     connector ``crm`` with params ``{...}``"). This tool does NOT execute
#     anything: it validates the inputs (identity present; connector / action /
#     summary / reason non-empty; params is an object) and then DELEGATES to
#     ``ee.cloud.external_actions.propose.propose_external_action``, which files
#     an Instinct Action carrying the ``_external_action`` blob and opens the
#     Decision-Graph chain. A human approves it in The Tray; ONLY then does the
#     apply-on-approve executor (ee/cloud/external_actions/executor.py) make the
#     connector call. Propose-only — the tool never fires the connector itself.
#
# Why delegate (vs. rebuilding belt's blob-build inline): the propose half
# already builds the blob, computes the params hash, mints the chain
# correlation_id, emits ``agent.proposed``, and back-writes the chain ids. This
# server is the thin agent-facing adapter over it — resolve identity, validate,
# call, relay. No connector secret ever passes through here.
#
# Security: ``params`` is DATA — it is passed verbatim to the propose helper
# (which hashes it and stores it in the Action blob) and never interpolated into
# a shell or eval'd. The connector NAME + scope are stored; the credential is
# resolved fresh at execution by the cloud connector service. NO phantom
# successes: the tool returns ok only after the propose helper returns a durable
# action id. Params content is never logged — only the connector + action +
# action id.
#
# EE→OSS boundary: this module lives in pocketpaw_ee; the surface service loads
# the tool ids as a plain frozenset[str] inside a try/except (never importing a
# pocketpaw_ee symbol into src/pocketpaw), exactly as it does for BELT_TOOL_IDS.
"""Agent-side MCP surface for the gated external-action proposal type."""

from __future__ import annotations

import json
import logging
from typing import Any

from ._audit import record_tool_call

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_external_actions"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form. Keep this id stable — the surface
# allowlist machinery matches it.
PROPOSE_EXTERNAL_ACTION_TOOL_ID = f"mcp__{SERVER_NAME}__propose_external_action"

EXTERNAL_ACTIONS_TOOL_IDS = (PROPOSE_EXTERNAL_ACTION_TOOL_ID,)


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


def _identity() -> tuple[str | None, str | None, str | None]:
    """Resolve the active workspace + user + session id from the per-stream
    ContextVars set by the cloud chat agent runtime. Returns
    ``(workspace_id, user_id, session_mongo_id)``."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import (
            current_session_mongo_id,
            current_user_id,
            current_workspace_id,
        )

        return current_workspace_id(), current_user_id(), current_session_mongo_id()
    except Exception:  # noqa: BLE001
        return None, None, None


async def _propose_external_action_handler(args: dict) -> dict:
    """MCP handler for ``external_actions__propose_external_action``.

    Resolves identity, validates inputs, then DELEGATES to
    ``ee.cloud.external_actions.propose.propose_external_action`` — which files
    the Instinct Action carrying the ``_external_action`` blob and opens the
    Decision-Graph chain. Returns ``{action_id, status: "pending_approval",
    summary}`` on success, or an ``is_error`` response with a plain relayable
    reason. NO phantom successes: ok is returned only after the propose helper
    hands back a durable action id. This handler NEVER executes the connector —
    propose only.
    """
    workspace_id, user_id, _session_mongo_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "propose_external_action requires workspace and user context "
            "(call from a cloud chat session)."
        )

    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        tool_server="pocketpaw_external_actions",
        tool_name="_propose_external_action",
        status="ok",
        ok=True,
    )

    connector = args.get("connector")
    action = args.get("action")
    params = args.get("params")
    summary = args.get("summary")
    reason = args.get("reason")

    if not isinstance(connector, str) or not connector.strip():
        return _error_response(
            "propose_external_action requires a non-empty `connector` (the bound "
            "connector name to call, e.g. 'crm')."
        )
    if not isinstance(action, str) or not action.strip():
        return _error_response(
            "propose_external_action requires a non-empty `action` (the named "
            "connector action to call, e.g. 'approveApplication')."
        )
    if not isinstance(summary, str) or not summary.strip():
        return _error_response(
            "propose_external_action requires a non-empty `summary` (a one-line "
            "human-readable description of the call for the approval gate)."
        )
    if not isinstance(reason, str) or not reason.strip():
        return _error_response(
            "propose_external_action requires a non-empty `reason` (why this call "
            "should be made — surfaced to the human reviewer in The Tray)."
        )
    # ``params`` is the proposed call payload — it must be a JSON object (or
    # absent, treated as empty). Reject a non-object so a malformed call is
    # refused before anything is filed.
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return _error_response(
            "propose_external_action `params` must be a JSON object (the call "
            "parameters passed verbatim to the connector action)."
        )

    # The gate one-liner the human sees in The Tray. Compose the agent's own
    # ``summary`` (what) with its ``reason`` (why) so the reviewer has both on
    # the Action's surface. The propose helper stores this on the blob's
    # ``summary`` field and in the Action recommendation.
    gate_summary = f"{summary.strip()} — {reason.strip()}"

    try:
        from pocketpaw_ee.cloud.external_actions.propose import propose_external_action
    except ImportError as exc:  # pragma: no cover — ee always present when this runs
        logger.warning("external_actions: propose import failed", exc_info=True)
        return _error_response(f"external-action proposals are unavailable: {exc}")

    try:
        action_id = await propose_external_action(
            workspace_id=workspace_id,
            connector_name=connector.strip(),
            action=action.strip(),
            params=params,
            requested_by=user_id,
            summary=gate_summary,
        )
    except ValueError as exc:
        # A validation error from the propose helper (e.g. an empty field that
        # slipped past) — relay it plainly; nothing was stored.
        return _error_response(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "external_actions: propose raised for connector=%s action=%s",
            connector,
            action,
            exc_info=True,
        )
        return _error_response(f"could not propose the external action: {exc}")

    if not action_id:
        return _error_response("the external action was not stored — please retry.")

    logger.info(
        "external_actions: proposed call '%s' on connector '%s' → Instinct action %s "
        "(workspace=%s)",
        action.strip(),
        connector.strip(),
        action_id,
        workspace_id,
    )

    return _success_response(
        {
            "action_id": action_id,
            "status": "pending_approval",
            "summary": gate_summary,
        }
    )


def build_external_actions_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for the external-action gate, or
    return ``None`` if the Claude Agent SDK isn't installed.

    Matches the shape returned by ``build_belt_server`` (``(name, server)`` or
    ``None``) so the backend's MCP registration loop treats it identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_external_actions MCP disabled")
        return None

    @tool(
        "propose_external_action",
        (
            "Propose a call to an EXTERNAL system through a bound connector, for "
            "HUMAN APPROVAL. Call this when the user wants you to take an action "
            "in an external tool (a CRM, a ticketing system, etc.) via a "
            "connector. This does NOT execute anything: it files the proposed "
            "call in The Tray for a human to approve or reject. ONLY on approval "
            "does the connector call actually fire — you never run it yourself. "
            "Args: `connector` (the bound connector name, e.g. 'crm'), `action` "
            "(the named connector action, e.g. 'approveApplication'), `params` "
            "(a JSON object of call parameters, passed verbatim to the action), "
            "`summary` (a one-line human-readable description of the call), "
            "`reason` (why this call should be made — shown to the reviewer). "
            "Returns {action_id, status:'pending_approval', summary}. An error "
            "means relay the reason — do NOT claim the action ran or succeeded; "
            "it is only PROPOSED until a human approves it."
        ),
        {
            "type": "object",
            "properties": {
                "connector": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The bound connector name to call (e.g. 'crm').",
                },
                "action": {
                    "type": "string",
                    "minLength": 1,
                    "description": "The named connector action (e.g. 'approveApplication').",
                },
                "params": {
                    "type": "object",
                    "description": "Call parameters, passed verbatim to the connector action.",
                },
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": "One-line human-readable description of the call.",
                },
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Why this call should be made (shown to the reviewer).",
                },
            },
            "required": ["connector", "action", "params", "summary", "reason"],
            "additionalProperties": False,
        },
    )
    async def propose_external_action(args):  # type: ignore[no-untyped-def]
        return await _propose_external_action_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[propose_external_action],
    )
    return SERVER_NAME, server


__all__ = [
    "EXTERNAL_ACTIONS_TOOL_IDS",
    "PROPOSE_EXTERNAL_ACTION_TOOL_ID",
    "SERVER_NAME",
    "build_external_actions_server",
]
