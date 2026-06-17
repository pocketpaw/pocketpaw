# connectors.py — in-process MCP server letting the cloud chat agent EXECUTE a
#   pocket's bound connectors.
# Created: 2026-06-08 (feat/connector-mcp-execution / keystone) — the missing
#   tool surface. M3 wired connector→SKILL (the agent learns HOW to use a
#   connector) but gave the cloud run no way to actually CALL connector actions —
#   the connector tools existed only on the local CLI path. This server closes
#   that: two tools, ``list_connector_actions`` (what can I do in this room?) and
#   ``connector_execute`` (do a READ action). v1 is READ-FIRST: ``auto``-trust
#   actions execute via the existing cloud ``connectors.service.execute`` path;
#   ``confirm`` / ``restricted`` (write-shaped) actions are listed but BLOCKED
#   with a "needs approval — coming in v2" refusal and are NEVER executed.
#   Identity (workspace / user / pocket) comes from the per-stream ContextVars in
#   ``ee.cloud.chat.agent_service`` — the same chokepoint the tasks / pockets /
#   sites servers use. Tool ids namespace as ``mcp__pocketpaw_connectors__*``.
#   OSS-EE boundary: this module imports only ``connectors.service`` (which owns
#   the Beanie read), never the WorkspaceConnector doc directly.
# Updated: 2026-06-08 (feat/sense-mcp / Sense tier chunk 4) — added the Sense
#   agent surface alongside the connector surface: two more tools,
#   ``list_senses`` (which provider-agnostic capabilities resolve in this
#   pocket?) and ``sense_execute`` (run a READ action against a Sense without
#   naming the connector). Both delegate to ``cloud.senses.resolver`` —
#   ``list_senses`` resolves each CORE_SENSES entry via ``resolve`` and reports
#   the bound connector; ``sense_execute`` calls ``execute_sense``, which OWNS
#   the read-first gate (only trust=auto runs) so this module does NOT re-gate.
#   Tool ids namespace as ``mcp__pocketpaw_connectors__list_senses`` /
#   ``…__sense_execute``.
# Updated: 2026-06-12 (workspace-scope reach) — unanchored chats (DM/group
#   threads with ``pocket_id=None``) no longer short-circuit to "no pocket":
#   both tools now fall through to the service with the workspace identity, so
#   WORKSPACE-scoped connectors are listable and executable from any chat in
#   the tenant. Pocket-scoped connectors stay room-private (the service's
#   pocket arm matches nothing for a null pocket). Execute uses
#   ``scope="workspace"`` credentials when unanchored. Trust gate unchanged —
#   writes still refuse pre-execute.
"""Agent-side MCP surface for executing a chat's reachable connectors.

Tools registered:

  - ``list_connector_actions()`` — for the CURRENT chat, list each reachable
    connector and its READ actions (``trust=auto``) the agent may call, plus
    its WRITE actions flagged "(needs approval — v2, blocked)". Reachable =
    enabled pocket-scoped connectors of the chat's pocket (when anchored) plus
    the workspace-scoped connectors of the tenant (always). No connectors → a
    clear message rather than a silent empty list.
  - ``connector_execute(connector_name, action, params)`` — run ONE read action.
    Gates in order: (a) connector must be enabled for this chat's reach (bound
    to THIS pocket, or workspace-scoped in THIS workspace); (b) the action's
    trust level decides read vs write; (c) ``auto`` → call
    ``connectors.service.execute`` and return the result; (d) ``confirm`` /
    ``restricted`` → refuse with the v2 message, never executing the write.

Identity comes from ``agent_service.current_workspace_id`` /
``current_user_id`` / ``current_pocket_id``. Outside an SSE chat stream those
are empty → the tools return a clear error rather than silently mis-tenanting.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_connectors"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
LIST_CONNECTOR_ACTIONS_TOOL_ID = f"mcp__{SERVER_NAME}__list_connector_actions"
CONNECTOR_EXECUTE_TOOL_ID = f"mcp__{SERVER_NAME}__connector_execute"
LIST_SENSES_TOOL_ID = f"mcp__{SERVER_NAME}__list_senses"
SENSE_EXECUTE_TOOL_ID = f"mcp__{SERVER_NAME}__sense_execute"

CONNECTOR_TOOL_IDS = (
    LIST_CONNECTOR_ACTIONS_TOOL_ID,
    CONNECTOR_EXECUTE_TOOL_ID,
    LIST_SENSES_TOOL_ID,
    SENSE_EXECUTE_TOOL_ID,
)

# The agent-facing refusal for any write/confirm-trust action in v1. Kept as a
# constant so the test asserts the exact contract and the skills quote it.
_V2_BLOCK_MESSAGE = (
    "This action modifies {connector} and needs approval (coming in v2). Not executed."
)


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape the SDK expects."""
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
    """Resolve workspace / user / pocket from the per-stream ContextVars set by
    ``run_core`` via ``agent_service.attach_agent_identity``.

    Returns ``(workspace_id, user_id, pocket_id)``. Any may be ``None`` when the
    tool is called outside an SSE chat stream (e.g. a unit test) — the handlers
    treat a missing workspace/pocket as "no room context".
    """
    try:
        from pocketpaw_ee.cloud.chat.agent_service import (
            current_pocket_id,
            current_user_id,
            current_workspace_id,
        )

        return current_workspace_id(), current_user_id(), current_pocket_id()
    except Exception:  # noqa: BLE001 — agent_service import only fails off-stream
        return None, None, None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _list_connector_actions_handler(args: dict) -> dict:  # noqa: ARG001 — no args
    workspace_id, _user_id, pocket_id = _identity()
    if not workspace_id:
        return _error_response(
            "no active workspace — list_connector_actions can only be called "
            "from inside a cloud chat stream"
        )
    from pocketpaw_ee.cloud.connectors import service as connectors_service

    try:
        # Unanchored chats (pocket_id=None) pass "" — the pocket arm of the
        # service query matches nothing, so only workspace-scoped connectors
        # come back. Anchored chats get pocket-scoped + workspace-scoped.
        connectors = await connectors_service.list_pocket_connectors(workspace_id, pocket_id or "")
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_connector_actions failed", exc_info=True)
        return _error_response(f"list_connector_actions failed: {exc}")

    if not connectors:
        return _success_response(
            {
                "pocket_id": pocket_id,
                "connectors": [],
                "message": (
                    "No connectors are reachable from this chat — none bound to "
                    "this pocket and none enabled workspace-wide. Enable a "
                    "connector (workspace scope) or bind one to this pocket to "
                    "use its actions here."
                ),
            }
        )

    out: list[dict[str, Any]] = []
    for c in connectors:
        read_actions = [
            {"action": a.name, "description": a.description} for a in c.actions if a.is_read
        ]
        write_actions = [
            {
                "action": a.name,
                "description": a.description,
                "status": "needs approval — v2, blocked",
            }
            for a in c.actions
            if not a.is_read
        ]
        out.append(
            {
                "connector": c.name,
                "display_name": c.display_name,
                "type": c.type,
                "read_actions": read_actions,
                "write_actions_blocked": write_actions,
            }
        )

    return _success_response(
        {
            "pocket_id": pocket_id,
            "connectors": out,
            "note": (
                "Call connector_execute(connector_name, action, params) to run a "
                "READ action. Write actions are blocked in v1 (need approval)."
            ),
        }
    )


async def _connector_execute_handler(args: dict) -> dict:
    workspace_id, user_id, pocket_id = _identity()
    if not workspace_id:
        return _error_response(
            "no active workspace — connector_execute can only be called from "
            "inside a cloud chat stream"
        )
    connector_name = args.get("connector_name")
    if not isinstance(connector_name, str) or not connector_name:
        return _error_response("connector_name is required (string)")
    action = args.get("action")
    if not isinstance(action, str) or not action:
        return _error_response("action is required (string)")
    params = args.get("params") or {}
    if not isinstance(params, dict):
        return _error_response("params must be an object (dict)")

    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.connectors import service as connectors_service
    from pocketpaw_ee.cloud.connectors.dto import ExecuteActionRequest

    # Gate 1 — the connector must be reachable from THIS chat: bound to this
    # pocket, or enabled workspace-wide in this workspace. An agent in pocket A
    # must never reach a connector bound only to pocket B; workspace scope is
    # the tenant boundary, so it passes from any chat (anchored or not).
    try:
        bound = await connectors_service.is_connector_bound_to_pocket(
            workspace_id, pocket_id or "", connector_name
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("connector bind check failed", exc_info=True)
        return _error_response(f"connector_execute failed: {exc}")
    if not bound:
        return _error_response(
            f"connector '{connector_name}' is not reachable from this chat — "
            "not bound to this pocket and not workspace-enabled. "
            "Call list_connector_actions to see what's available here."
        )

    # Gate 2 — look up the action's trust level. Unknown action → clear error.
    trust = await connectors_service.get_action_trust(connector_name, action)
    if trust is None:
        return _error_response(
            f"connector '{connector_name}' has no action '{action}'. "
            "Call list_connector_actions for the available actions."
        )

    # Gate 3 — WRITE (confirm/restricted) actions are blocked in v1. Refuse
    # BEFORE any execute call so a write can never run.
    if not trust.is_read:
        return _success_response(
            {
                "executed": False,
                "blocked": True,
                "connector": connector_name,
                "action": action,
                "trust_level": trust.trust_level,
                "reason": _V2_BLOCK_MESSAGE.format(connector=connector_name),
            }
        )

    # Gate 4 — READ (auto-trust): run via the existing cloud execute path. It
    # uses the connector's stored config (PAT/token) through the
    # DirectRESTAdapter / native adapter — no OAuth flow in v1. Anchored chats
    # execute with the pocket's credentials; unanchored chats fall back to the
    # workspace scope's credentials.
    body = ExecuteActionRequest(
        action=action,
        params=params,
        scope="pocket" if pocket_id else "workspace",
        pocket_id=pocket_id or None,
    )
    try:
        result = await connectors_service.execute(
            workspace_id, connector_name, body, user_id=user_id
        )
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("connector_execute failed", exc_info=True)
        return _error_response(f"connector_execute failed: {exc}")

    return _success_response(
        {
            "executed": True,
            "connector": connector_name,
            "action": action,
            "success": result.success,
            "data": result.data,
            "error": result.error,
            "records_affected": result.records_affected,
            "execution_mode": result.execution_mode,
        }
    )


# ---------------------------------------------------------------------------
# Tool handlers — Sense tier (provider-agnostic capabilities above connectors)
# ---------------------------------------------------------------------------


async def _list_senses_handler(args: dict) -> dict:  # noqa: ARG001 — no args
    workspace_id, _user_id, pocket_id = _identity()
    if not workspace_id:
        return _error_response(
            "no active workspace — list_senses can only be called from inside a cloud chat stream"
        )

    from pocketpaw.senses import CORE_SENSES
    from pocketpaw_ee.cloud.senses.resolver import resolve_many

    # One enabled-connector read for all CORE_SENSES (was one query per sense).
    try:
        resolved_map = await resolve_many(
            [s.id for s in CORE_SENSES], workspace_id, pocket_id=pocket_id
        )
    except Exception as exc:  # noqa: BLE001 — never let one sense break the list
        logger.warning("list_senses: resolve_many failed", exc_info=True)
        return _error_response(f"list_senses failed: {exc}")

    senses_out: list[dict[str, Any]] = []
    for sense in CORE_SENSES:
        resolved = resolved_map.get(sense.id)
        if resolved is None:
            # No enabled connector fills this sense for the workspace — skip it
            # so the agent only sees capabilities it can actually use.
            continue
        senses_out.append(
            {
                "sense": sense.id,
                "display_name": sense.display_name,
                "description": sense.description,
                "connector": resolved.connector_name,
                "ambiguous": resolved.ambiguous,
                "candidates": resolved.candidates,
            }
        )

    if not senses_out:
        return _success_response(
            {
                "pocket_id": pocket_id,
                "senses": [],
                "message": (
                    "No capabilities (Senses) are available here yet — no enabled "
                    "connector fills any core sense for this workspace. Connect a "
                    "provider (email, calendar, code, etc.) to use senses."
                ),
            }
        )

    return _success_response(
        {
            "pocket_id": pocket_id,
            "senses": senses_out,
            "note": (
                "Call sense_execute(sense, action, params) to run a READ action "
                "against a capability without naming the connector. If a sense is "
                "ambiguous, the resolver picked the first candidate — the user can "
                "set a preference to disambiguate. Write actions are blocked in v1."
            ),
        }
    )


async def _sense_execute_handler(args: dict) -> dict:
    workspace_id, user_id, pocket_id = _identity()
    if not workspace_id:
        return _error_response(
            "no active workspace — sense_execute can only be called from inside a cloud chat stream"
        )
    # No pocket guard: ``execute_sense`` takes ``pocket_id=None`` natively and
    # resolves workspace-scoped providers, matching ``_list_senses_handler`` —
    # a sense the agent can list must also be executable from the same chat.

    sense = args.get("sense")
    if not isinstance(sense, str) or not sense:
        return _error_response("sense is required (string, e.g. 'paw.email.v1')")
    action = args.get("action")
    if not isinstance(action, str) or not action:
        return _error_response("action is required (string)")
    params = args.get("params") or {}
    if not isinstance(params, dict):
        return _error_response("params must be an object (dict)")

    from pocketpaw.senses import SenseValidationError
    from pocketpaw_ee.cloud.senses.resolver import execute_sense

    try:
        result = await execute_sense(
            sense,
            action,
            params,
            workspace_id,
            pocket_id=pocket_id,
            user_id=user_id,
        )
    except SenseValidationError as exc:
        return _error_response(f"unknown sense {sense!r}: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("sense_execute failed", exc_info=True)
        return _error_response(f"sense_execute failed: {exc}")

    # execute_sense OWNS the read-first gate. A False result is a structured
    # refusal (sense.no_provider / sense.action_needs_approval), not a crash.
    if not result.ok:
        return _error_response(result.message or result.error or "sense_execute refused")

    # result.data is the underlying ExecuteActionResponse. Flatten it into the
    # SAME shape connector_execute returns (json.dumps uses default=str, so a
    # nested Pydantic model would serialize as an opaque repr the agent can't
    # read) — keep the two tools' success payloads structurally identical.
    resp = result.data
    return _success_response(
        {
            "executed": True,
            "sense": result.sense_id,
            "connector": result.connector_name,
            "action": result.action,
            "success": getattr(resp, "success", True),
            "data": getattr(resp, "data", None),
            "error": getattr(resp, "error", None),
            "records_affected": getattr(resp, "records_affected", 0),
            "execution_mode": getattr(resp, "execution_mode", "cloud"),
        }
    )


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def build_connectors_context_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for connector execution, or return
    ``None`` if the Claude Agent SDK isn't installed.

    Matches the ``(name, server)`` shape the other servers return so the
    backend's MCP registration loop in ``claude_sdk.py`` treats them uniformly.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_connectors MCP disabled")
        return None

    @tool(
        "list_connector_actions",
        (
            "List the connectors bound to the CURRENT pocket/room and the "
            "actions you can run on each. Call this FIRST whenever the user "
            "wants to read from an integration (GitHub issues/PRs, Gmail "
            "search, etc.) so you know which connector + action to use. "
            "Returns each connector's READ actions (which you may run directly "
            "via connector_execute) and its WRITE actions, which are listed but "
            "BLOCKED in v1 (they need approval). No arguments — the pocket is "
            "inferred from the active chat. If the chat isn't in a pocket, or "
            "the pocket has no bound connectors, the result says so."
        ),
        {},
    )
    async def list_connector_actions(args):  # type: ignore[no-untyped-def]
        return await _list_connector_actions_handler(args)

    @tool(
        "connector_execute",
        (
            "Execute ONE connector action for the current pocket. Use this to "
            "READ from a bound integration — e.g. list a GitHub repo's issues "
            "or PRs, search Gmail. Args: `connector_name` (e.g. 'github', "
            "'gmail'), `action` (an action name from list_connector_actions, "
            "e.g. 'list_issues', 'gmail_search'), and `params` (an object of "
            "that action's parameters). Only READ (auto-trust) actions run; "
            "WRITE actions (create/send/modify/delete) are refused with a "
            "'needs approval — coming in v2' message and are NEVER executed. "
            "The connector must be bound to THIS pocket and have a token in its "
            "config; otherwise you get a clear error. Always call "
            "list_connector_actions first to pick a valid connector + action."
        ),
        {
            "type": "object",
            "properties": {
                "connector_name": {
                    "type": "string",
                    "description": "Registry name of the connector, e.g. 'github' or 'gmail'.",
                },
                "action": {
                    "type": "string",
                    "description": (
                        "Action name from list_connector_actions, e.g. "
                        "'list_issues' or 'gmail_search'. Read actions only."
                    ),
                },
                "params": {
                    "type": "object",
                    "description": "Object of the action's parameters (may be empty).",
                },
            },
            "required": ["connector_name", "action"],
        },
    )
    async def connector_execute(args):  # type: ignore[no-untyped-def]
        return await _connector_execute_handler(args)

    @tool(
        "list_senses",
        (
            "List the capabilities (Senses) you can use in the CURRENT "
            "pocket/room. A Sense is a provider-agnostic capability (e.g. "
            "'paw.email.v1' = email, 'paw.code.v1' = repos/issues/PRs) that the "
            "resolver binds to whichever connector the tenant enabled — so you "
            "address the capability, not a specific provider. Call this FIRST "
            "when the user asks for something by capability ('check my email', "
            "'what's on my calendar') rather than by provider name. Returns only "
            "senses that resolve to a connector here, each with the bound "
            "`connector`, whether the choice was `ambiguous` (more than one "
            "provider, no preference set), and the `candidates`. No arguments — "
            "the pocket is inferred from the active chat. Then call sense_execute "
            "to run a READ action."
        ),
        {},
    )
    async def list_senses(args):  # type: ignore[no-untyped-def]
        return await _list_senses_handler(args)

    @tool(
        "sense_execute",
        (
            "Run ONE READ action against a capability (Sense) for the current "
            "pocket WITHOUT naming the provider. Use this when the user asks by "
            "capability ('search my email for the invoice', 'list my open PRs') "
            "and you want the resolver to pick the connector the tenant enabled. "
            "Args: `sense` (a sense id from list_senses, e.g. 'paw.email.v1'), "
            "`action` (an action name, e.g. 'gmail_search' or 'list_issues'), and "
            "`params` (an object of that action's parameters). READ-FIRST: only "
            "read (auto-trust) actions run; WRITE actions (create/send/modify/"
            "delete) are refused with a 'needs approval' message and are NEVER "
            "executed. If no connector fills the sense for this workspace you get "
            "a clear 'no provider' error. Always call list_senses first to see "
            "which senses resolve and pick a valid action."
        ),
        {
            "type": "object",
            "properties": {
                "sense": {
                    "type": "string",
                    "description": (
                        "Sense id from list_senses, e.g. 'paw.email.v1' or "
                        "'paw.code.v1'. Names the capability, not a connector."
                    ),
                },
                "action": {
                    "type": "string",
                    "description": (
                        "Action name to run, e.g. 'gmail_search' or 'list_issues'. "
                        "Read actions only — writes are refused."
                    ),
                },
                "params": {
                    "type": "object",
                    "description": "Object of the action's parameters (may be empty).",
                },
            },
            "required": ["sense", "action"],
        },
    )
    async def sense_execute(args):  # type: ignore[no-untyped-def]
        return await _sense_execute_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.1.0",
        tools=[list_connector_actions, connector_execute, list_senses, sense_execute],
    )
    return SERVER_NAME, server


__all__ = [
    "CONNECTOR_EXECUTE_TOOL_ID",
    "CONNECTOR_TOOL_IDS",
    "LIST_CONNECTOR_ACTIONS_TOOL_ID",
    "LIST_SENSES_TOOL_ID",
    "SENSE_EXECUTE_TOOL_ID",
    "SERVER_NAME",
    "build_connectors_context_server",
]
