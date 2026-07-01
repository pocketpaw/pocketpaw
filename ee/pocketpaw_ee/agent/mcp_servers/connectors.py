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
# Updated: 2026-06-15 (feat/invoke-tool-v1, v2) — WRITE PATH unlocked via
#   Instinct, making THIS chat-agent surface SYMMETRIC with the flow-button
#   surface (``cloud.pockets.tool_executor._propose_connector_write``, same
#   branch). ``connector_execute``'s Gate 3 no longer refuses a WRITE with the
#   "coming in v2" stub. A WRITE action (``trust.is_read`` False — confirm /
#   restricted) is now PROPOSED to a human through the existing external-action
#   gate: it calls ``external_actions.propose.propose_external_action(...)``
#   (which files a PENDING Instinct ``Action`` carrying the ``_external_action``
#   blob: params_hash + idempotency_key, NO connector secret) and returns a
#   pending-shaped success ``{ok:true, status:"pending_approval", action_id,
#   message}`` so the agent relays "I've proposed that change — approve it in
#   your Tray" to the user.
#   THE load-bearing v2 security rule (identical to tool_executor): a WRITE
#   STILL never calls ``connectors.service.execute`` INLINE — the human gates
#   it. The connector write fires only later, when a human approves in The Tray
#   and the instinct router runs ``execute_approved_external_action`` →
#   ``connectors.service.execute`` (re-validated: workspace + params_hash +
#   idempotency). This module adds NOTHING to that approve→execute path; it only
#   proposes-and-suspends. READS are unchanged (auto-trust still fires execute()
#   directly). ``list_connector_actions`` now reports write actions as
#   "needs approval — proposes to your Tray" rather than "blocked".
#   IMPORT-LINTER: ``propose_external_action`` is lazy-imported inside the WRITE
#   branch (mirroring how the READ path already lazy-imports ``connectors.service``
#   function-locally), so the module's import surface is unchanged and the cloud
#   chokepoint contracts stay 0-broken. (``propose.py`` itself statically imports
#   no Beanie document class — it lazy-imports ``pocketpaw.stores`` /
#   ``pocketpaw.instinct.models`` internally.)
"""Agent-side MCP surface for executing a chat's reachable connectors.

Tools registered:

  - ``list_connector_actions()`` — for the CURRENT chat, list each reachable
    connector and its READ actions (``trust=auto``) the agent may call, plus
    its WRITE actions flagged "(needs approval — proposes to your Tray)".
    Reachable = enabled pocket-scoped connectors of the chat's pocket (when
    anchored) plus the workspace-scoped connectors of the tenant (always). No
    connectors → a clear message rather than a silent empty list.
  - ``connector_execute(connector_name, action, params)`` — run ONE read action
    OR propose ONE write. Gates in order: (a) connector must be enabled for this
    chat's reach (bound to THIS pocket, or workspace-scoped in THIS workspace);
    (b) the action's trust level decides read vs write; (c) ``auto`` → call
    ``connectors.service.execute`` and return the result; (d) ``confirm`` /
    ``restricted`` (write) → PROPOSE the call through the Instinct external-
    action gate (``propose_external_action``) and return a pending result with
    an ``action_id`` — the write is NEVER executed inline; it fires only when a
    human approves it in The Tray. This makes the chat-agent surface symmetric
    with the flow-button surface (``cloud.pockets.tool_executor``).

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
# Audit helpers for connector tool execution (chat agent tools path).
# Added: 2026-06-22 — the MCP connector_execute / sense_execute handlers
#   call ``connectors.service.execute`` directly, which has NO audit
#   instrumentation. These helpers write a runtime audit event (SQLite via
#   get_audit_logger) AND a workspace audit event (MongoDB via
#   audit_service.record) so agent connector activity shows up in the
#   activity feed under the "tool" category. Failures are logged and
#   swallowed so they never break the tool call.
# ---------------------------------------------------------------------------


def _audit_connector_execute(
    *,
    workspace_id: str,
    user_id: str | None,
    pocket_id: str | None,
    connector_name: str,
    action: str,
    status: str,
    ok: bool = True,
    via_sense: str | None = None,
) -> None:
    """Write audit events for a connector execute (READ) or propose (WRITE).

    Two sinks: runtime audit (SQLite, raw event) + workspace audit (MongoDB,
    rich detail). Never raises.
    """
    actor_id = user_id or "agent"

    # 1. Runtime audit via get_audit_logger
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        severity = AuditSeverity.INFO if ok else AuditSeverity.WARNING
        get_audit_logger().log(
            AuditEvent.create(
                severity=severity,
                actor=actor_id,
                action="connector.execute",
                target=connector_name,
                status=status,
                category="pocket_tool_run",
                workspace_id=workspace_id,
                connector_action=action,
                pocket_id=pocket_id or "",
                via_sense=via_sense or "",
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the tool
        logger.warning("connector-execute runtime audit failed", exc_info=True)

    # 2. Workspace audit via audit_service.record (fire-and-forget)
    try:
        import asyncio

        from pocketpaw_ee.cloud.audit import service as _audit_service

        target_id = f"{connector_name}.{action}"
        asyncio.ensure_future(
            _audit_service.record(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="workspace.agent.tool_executed",
                target_type="connector",
                target_id=target_id,
                metadata={
                    "connector": connector_name,
                    "connector_action": action,
                    "pocket_id": pocket_id or "",
                    "status": status,
                    "ok": ok,
                    "via_sense": via_sense or "",
                    "source": "chat_page",
                },
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the tool
        logger.warning("connector-execute workspace-audit record failed", exc_info=True)


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
                "status": "needs approval — proposes to your Tray",
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
                "write_actions_need_approval": write_actions,
            }
        )

    return _success_response(
        {
            "pocket_id": pocket_id,
            "connectors": out,
            "note": (
                "Call connector_execute(connector_name, action, params) to run a "
                "READ action (fires immediately) OR to propose a WRITE action. A "
                "write isn't executed inline — it's sent to the user's Tray for "
                "approval and runs only once they approve. Tell the user you've "
                "proposed it and ask them to approve it in their Tray."
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

    # Gate 3 — WRITE (confirm/restricted) actions PROPOSE through Instinct (v2).
    # THE load-bearing security rule: a WRITE action NEVER calls execute()
    # INLINE. We route it through the external-action gate (`propose_external_
    # action`), which files a PENDING Instinct Action carrying the
    # `_external_action` blob (params_hash + idempotency_key, no connector
    # secret) and opens the Decision-Graph chain. The connector write fires only
    # when a human approves in The Tray — the instinct router then runs
    # `execute_approved_external_action` → `connectors.service.execute` (re-
    # validated: workspace + params_hash + idempotency). We add NOTHING to that
    # approve→execute path here; we propose-and-suspend, then STOP. This mirrors
    # `cloud.pockets.tool_executor._propose_connector_write` EXACTLY so both the
    # chat-agent surface and the flow-button surface gate writes the same way.
    if not trust.is_read:
        return await _propose_connector_write(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            connector_name=connector_name,
            action=action,
            params=params,
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
        _audit_connector_execute(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            connector_name=connector_name,
            action=action,
            status=exc.code,
            ok=False,
        )
        return _error_response(f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("connector_execute failed", exc_info=True)
        _audit_connector_execute(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            connector_name=connector_name,
            action=action,
            status="error",
            ok=False,
        )
        return _error_response(f"connector_execute failed: {exc}")

    _audit_connector_execute(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        connector_name=connector_name,
        action=action,
        status="ok",
        ok=result.success,
    )

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


async def _propose_connector_write(
    *,
    workspace_id: str,
    user_id: str | None,
    pocket_id: str | None,
    connector_name: str,
    action: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """Gate 3 WRITE path (v2) — propose the connector write to a human, suspend.

    Files a PENDING Instinct ``Action`` via ``propose_external_action`` (the
    external-action gate, schema 1) and returns a pending-shaped success
    envelope so the chat agent relays "I've proposed that change — approve it in
    your Tray" to the user. THE load-bearing security rule: this NEVER calls
    ``connectors.service.execute``. The connector write fires only when a human
    approves in The Tray — the instinct router then runs
    ``execute_approved_external_action`` → ``connectors.service.execute`` (re-
    validated). We propose-and-suspend, STOP. Mirrors
    ``cloud.pockets.tool_executor._propose_connector_write`` so the chat-agent
    surface and the flow-button surface gate writes identically.

    Identity (``workspace_id`` / ``user_id`` / ``pocket_id``) is resolved by the
    caller from the same per-stream ContextVars the READ path reads. ``scope``
    follows the pocket: a pocket-anchored chat proposes a ``pocket``-scoped
    call; an unanchored chat proposes a ``workspace``-scoped one — matching the
    credentials the eventual approved execute resolves.
    """
    # Lazy import — keeps the module's import surface identical to today (the
    # READ path already lazy-imports ``connectors.service`` function-locally).
    # ``propose_external_action`` statically imports no Beanie document class, so
    # the cloud chokepoint contracts stay 0-broken.
    from pocketpaw_ee.cloud.external_actions.propose import propose_external_action

    scope = "pocket" if pocket_id else "workspace"
    try:
        action_id = await propose_external_action(
            workspace_id=workspace_id,
            connector_name=connector_name,
            action=action,
            params=params,
            requested_by=user_id or "",
            scope=scope,
            pocket_id=pocket_id or None,
            summary=f"{connector_name}.{action} from chat (pocket {pocket_id or '—'})",
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean MCP error, never a 500.
        logger.warning("connector_execute propose failed", exc_info=True)
        _audit_connector_execute(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            connector_name=connector_name,
            action=action,
            status="propose_failed",
            ok=False,
        )
        return _error_response(
            f"could not send '{action}' on {connector_name!r} for approval: {exc}"
        )

    logger.info(
        "connector_execute proposed write: connector=%r action=%r pocket=%r action_id=%s",
        connector_name,
        action,
        pocket_id,
        action_id,
    )
    _audit_connector_execute(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        connector_name=connector_name,
        action=action,
        status="pending_approval",
        ok=True,
    )
    # Pending-shaped success: the human gates the write. ``status`` is the
    # string ``"pending_approval"`` (not an HTTP code) so the agent reads it as
    # "awaiting approval"; ``action_id`` lets the user (and the agent) correlate
    # the proposal with the pending Action it watches in The Tray. The
    # ``message`` is phrased for the agent to relay verbatim.
    return _success_response(
        {
            "executed": False,
            "status": "pending_approval",
            "action_id": action_id,
            "connector": connector_name,
            "action": action,
            "message": (
                f"Proposed '{action}' on {connector_name} — it's waiting for your "
                "approval in the Tray. Approve it there to run it."
            ),
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
        _audit_connector_execute(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            connector_name=sense,
            action=action,
            status="unknown_sense",
            ok=False,
            via_sense=sense,
        )
        return _error_response(f"unknown sense {sense!r}: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("sense_execute failed", exc_info=True)
        _audit_connector_execute(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            connector_name=sense,
            action=action,
            status="error",
            ok=False,
            via_sense=sense,
        )
        return _error_response(f"sense_execute failed: {exc}")

    # execute_sense OWNS the read-first gate. A False result is a structured
    # refusal (sense.no_provider / sense.action_needs_approval), not a crash.
    if not result.ok:
        _audit_connector_execute(
            workspace_id=workspace_id,
            user_id=user_id,
            pocket_id=pocket_id,
            connector_name=result.connector_name or sense,
            action=action,
            status="refused",
            ok=False,
            via_sense=sense,
        )
        return _error_response(result.message or result.error or "sense_execute refused")

    _audit_connector_execute(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id,
        connector_name=result.connector_name or sense,
        action=action,
        status="ok",
        ok=True,
        via_sense=sense,
    )

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
            "wants to read from OR change something in an integration (GitHub "
            "issues/PRs, Gmail search/send, etc.) so you know which connector + "
            "action to use. Returns each connector's READ actions (which you may "
            "run directly via connector_execute) and its WRITE actions, which "
            "you may PROPOSE via connector_execute — a write isn't executed "
            "inline, it's sent to the user's Tray for approval and runs only "
            "once they approve. No arguments — the pocket is inferred from the "
            "active chat. If the chat isn't in a pocket, or the pocket has no "
            "bound connectors, the result says so."
        ),
        {},
    )
    async def list_connector_actions(args):  # type: ignore[no-untyped-def]
        return await _list_connector_actions_handler(args)

    @tool(
        "connector_execute",
        (
            "Run ONE connector action for the current pocket. Use this to READ "
            "from a bound integration (e.g. list a GitHub repo's issues or PRs, "
            "search Gmail) OR to make a CHANGE (create an issue, send an email, "
            "update a record). Args: `connector_name` (e.g. 'github', 'gmail'), "
            "`action` (an action name from list_connector_actions, e.g. "
            "'list_issues', 'create_issue', 'gmail_send'), and `params` (an "
            "object of that action's parameters). READ (auto-trust) actions run "
            "immediately and return their data. WRITE actions (create/send/"
            "modify/delete) are NOT executed inline — they are PROPOSED to the "
            "user for approval: the tool returns status 'pending_approval' with "
            "an action_id, and the change runs only once the user approves it in "
            "their Tray. When you get a pending result, tell the user you've "
            "proposed the change and ask them to approve it in their Tray — do "
            "NOT claim it's done. The connector must be bound to THIS pocket and "
            "have a token in its config; otherwise you get a clear error. Always "
            "call list_connector_actions first to pick a valid connector + action."
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
                        "'list_issues' (read) or 'create_issue' (write — proposed "
                        "for approval)."
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
