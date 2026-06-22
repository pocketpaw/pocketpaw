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
    actor_id = user_id or "agent"
    target_id = f"{tool_server}:{tool_name}" if tool_server else tool_name

    # 1. Runtime audit (SQLite) — category "pocket_tool_run" so the bridge
    #    maps it to the "tool" category for the activity feed.
    try:
        from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger

        severity = AuditSeverity.INFO if ok else AuditSeverity.WARNING
        get_audit_logger().log(
            AuditEvent.create(
                severity=severity,
                actor=actor_id,
                action="mcp.tool.call",
                target=target_id,
                status=status,
                category="pocket_tool_run",
                workspace_id=workspace_id,
                pocket_id=pocket_id or "",
                tool_server=tool_server,
                tool_name=tool_name,
                **(metadata or {}),
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the tool
        logger.warning("mcp-audit runtime failed for %s/%s", tool_server, tool_name, exc_info=True)

    # 2. Workspace audit (MongoDB) — rich detail for the /activity page.
    try:
        import asyncio

        from pocketpaw_ee.cloud.audit import service as _audit_service  # noqa: I001

        asyncio.ensure_future(
            _audit_service.record(
                workspace_id=workspace_id,
                actor_id=actor_id,
                action="workspace.agent.tool_executed",
                target_type="mcp_tool",
                target_id=target_id,
                metadata={
                    "tool_server": tool_server,
                    "tool_name": tool_name,
                    "pocket_id": pocket_id or "",
                    "status": status,
                    "ok": ok,
                    "source": "chat_page",
                    **(metadata or {}),
                },
            )
        )
    except Exception:  # noqa: BLE001 — audit must never break the tool
        logger.warning(
            "mcp-audit workspace failed for %s/%s", tool_server, tool_name, exc_info=True
        )
