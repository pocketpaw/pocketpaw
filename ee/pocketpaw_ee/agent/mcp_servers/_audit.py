# _audit.py — Shared audit helper for every MCP tool handler.
#
# Every in-process MCP tool the cloud agent calls from the chat page /
# pocket page should leave a forensic trail.  This helper writes to BOTH
# sinks — the runtime audit (SQLite via get_audit_logger, so the legacy
# GET /api/v1/audit surface sees it) AND the workspace audit (MongoDB
# via audit_service.record, so the rich GET /workspaces/{id}/audit
# surface shows it with full detail for the /activity page).
#
# Usage from any MCP handler::
#
#     _audit.record_tool_call(
#         workspace_id=ws_id,
#         user_id=user_id,
#         pocket_id=pocket_id,
#         tool_server="pocketpaw_fabric",
#         tool_name="fabric_query",
#         status="ok" if ok else "error",
#         ok=ok,
#         metadata={"query": query},
#     )
#
# Failures are logged and swallowed — audit must never break the tool.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def record_tool_call(
    *,
    workspace_id: str,
    user_id: str | None = None,
    pocket_id: str | None = None,
    tool_server: str,
    tool_name: str,
    status: str,
    ok: bool = True,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a chat-agent MCP tool call to both audit sinks.

    Parameters
    ----------
    workspace_id : str — always required; the tool runner is tenant-aware.
    user_id : str | None — the human who triggered the call.
    pocket_id : str | None — the pocket/room context, if any.
    tool_server : str — MCP server name (e.g. ``"pocketpaw_tasks"``).
    tool_name : str — tool function name (e.g. ``"claim_task"``).
    status : str — outcome (``"ok"``, ``"error"``, ``"pending_approval"``, …).
    ok : bool — whether the tool succeeded from the user's perspective.
    metadata : dict | None — extra context (never PII/tokens).
    """
    _record_audit_event(
        workspace_id=workspace_id,
        actor_id=user_id or "agent",
        target_id=f"{tool_server}:{tool_name}" if tool_server else tool_name,
        action="workspace.agent.tool_executed",
        target_type="mcp_tool",
        category="pocket_tool_run",
        runtime_action="mcp.tool.call",
        status=status,
        ok=ok,
        metadata={
            "tool_server": tool_server,
            "tool_name": tool_name,
            "pocket_id": pocket_id or "",
            "source": "chat_page",
            **(metadata or {}),
        },
    )


def record_decision(
    *,
    workspace_id: str,
    actor_id: str = "agent",
    pocket_id: str | None = None,
    decision_action: str,
    outcome: str,
    ok: bool = True,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record an agent decision event to the workspace audit.

    This writes decision-graph events (proposals, approvals, rejections,
    completions) to the activity feed under the "decision" category.

    Parameters
    ----------
    workspace_id : str — the tenant workspace.
    actor_id : str — who/what made the decision (default "agent").
    pocket_id : str | None — the pocket context, if any.
    decision_action : str — e.g. ``"agent.proposed"``, ``"human.approved"``,
        ``"decision.completed"``.
    outcome : str — ``"ok"``, ``"approved"``, ``"rejected"``, ``"completed"``.
    ok : bool — whether the decision succeeded.
    metadata : dict | None — extra context (correlation_id, etc.).
    """
    _record_audit_event(
        workspace_id=workspace_id,
        actor_id=actor_id,
        target_id=f"decision:{decision_action}",
        action="workspace.agent.decision",
        target_type="decision_event",
        category="pocket_router",
        runtime_action=f"decision.{decision_action}",
        status=outcome,
        ok=ok,
        metadata={
            "decision_action": decision_action,
            "pocket_id": pocket_id or "",
            "source": "agent",
            **(metadata or {}),
        },
    )


def record_system_event(
    *,
    workspace_id: str,
    actor_id: str = "system",
    pocket_id: str | None = None,
    event_type: str,
    description: str,
    status: str = "completed",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record a system-level agent event to the workspace audit.

    This writes agent lifecycle events (start/stop, connector sync,
    scheduled tasks) to the activity feed under the "system" category.

    Parameters
    ----------
    workspace_id : str — the tenant workspace.
    actor_id : str — the system component (default "system").
    pocket_id : str | None — the pocket context, if any.
    event_type : str — e.g. ``"agent.started"``, ``"connector.synced"``.
    description : str — human-readable summary for the activity feed.
    status : str — ``"completed"``, ``"failed"``, ``"running"``.
    metadata : dict | None — extra context.
    """
    _record_audit_event(
        workspace_id=workspace_id,
        actor_id=actor_id,
        target_id=event_type,
        action="workspace.agent.system",
        target_type="system_event",
        category="pocket_backend_config",
        runtime_action=event_type,
        status=status,
        ok=status == "completed",
        metadata={
            "event_type": event_type,
            "description": description,
            "pocket_id": pocket_id or "",
            "source": "system",
            **(metadata or {}),
        },
    )


# ---------------------------------------------------------------------------
# Internal — shared write path for all three event types
# ---------------------------------------------------------------------------


def _record_audit_event(
    *,
    workspace_id: str,
    actor_id: str,
    target_id: str,
    action: str,
    target_type: str,
    category: str,
    runtime_action: str,
    status: str,
    ok: bool = True,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Fire-and-forget write to both audit sinks.

    1. Runtime audit (SQLite) — the bridge listener mirrors this into the
       AuditStore, mapping ``category`` via ``_CATEGORY_MAP``.
    2. Workspace audit (MongoDB) — the primary source for the activity feed.
    """
    safe_meta = dict(metadata or {})

    # 1. Runtime audit (SQLite)
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        severity = AuditSeverity.INFO if ok else AuditSeverity.WARNING
        get_audit_logger().log(
            AuditEvent.create(
                severity=severity,
                actor=actor_id,
                action=runtime_action,
                target=target_id,
                status=status,
                category=category,
                workspace_id=workspace_id,
                **safe_meta,
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the caller
        logger.warning("audit runtime sink failed for %s", action, exc_info=True)

    # 2. Workspace audit (MongoDB)
    try:
        import asyncio

        from pocketpaw_ee.cloud.audit import service as _audit_service  # noqa: I001

        asyncio.ensure_future(
            _audit_service.record(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                metadata={
                    **safe_meta,
                    "category": category,  # frontend uses this for feed filter
                },
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the caller
        logger.warning("audit workspace sink failed for %s", action, exc_info=True)
