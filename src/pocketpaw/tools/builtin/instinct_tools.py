# Instinct tools — agent tools for the decision pipeline.
# Created: 2026-03-28 — Lets the agent propose actions, check pending, read audit.
# Updated: 2026-08-06 (C4-a — stop cross-tenant leakage) — all three tools are
#   now WORKSPACE-SCOPED, matching the in-process MCP sibling
#   (ee/pocketpaw_ee/agent/mcp_servers/instinct.py) which already resolved
#   tenancy from the per-stream agent identity and passed workspace_id into
#   every store call. The builtin path — the ONLY Instinct surface the non-SDK
#   backends (deep_agents, google_adk, openai_agents) see — passed none, so:
#     * ``instinct_propose`` wrote Actions with workspace_id = NULL. Because
#       ``_workspace_scope()`` renders a scoped read as
#       ``(workspace_id = ? OR workspace_id IS NULL)`` for W4a legacy
#       compatibility, a NULL Action was ACTIVELY RETURNED to every tenant's
#       scoped pending query on that file — it showed up in other tenants'
#       approval queues. Now the resolved workspace is stamped on the row
#       (and the calling user is recorded as the assignee), so the proposal is
#       visible only to its own tenant.
#     * ``instinct_pending`` / ``instinct_audit`` read unfiltered. Both now
#       pass workspace_id, so a tenant's queue and decision trail never
#       include another tenant's rows.
#   Fail-closed: when the POSITIVE per-run marker ``is_tenant_scoped_run()``
#   says this is a cloud chat dispatch and no workspace resolves, the tool
#   REFUSES instead of falling back to NULL/global scope. The marker is
#   deliberately not a process-global (see _tenancy.py for the #1570 rationale)
#   so workspace-less OSS / CLI / background runs keep working unchanged.

import logging
from typing import Any

from pocketpaw.tools.builtin._tenancy import (
    current_workspace,
    is_tenant_scoped_run,
    workspace_required_message,
)
from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


def _get_instinct_store():
    """Lazy import to avoid circular deps at module load."""
    try:
        from pocketpaw.stores import get_instinct_store

        return get_instinct_store()
    except ImportError:
        return None


def _resolve_scope(tool_name: str) -> tuple[str | None, str | None]:
    """Resolve ``(workspace_id, refusal)`` for one tool invocation.

    ``refusal`` is non-None when the run is tenant-scoped but no workspace
    resolved — the caller returns it verbatim and performs NO store access.
    """
    workspace = current_workspace()
    if workspace is None and is_tenant_scoped_run():
        logger.warning(
            "%s refused: tenant-scoped run with no resolvable workspace identity", tool_name
        )
        return None, workspace_required_message(tool_name)
    return workspace, None


class InstinctProposeTool(BaseTool):
    """Propose an action for human approval."""

    @property
    def name(self) -> str:
        return "instinct_propose"

    @property
    def description(self) -> str:
        return (
            "Propose an action that requires human approval before execution. "
            "Use this when you've analyzed data and want to recommend an action "
            "(e.g., 'reorder inventory', 'flag suspicious invoice', 'send reminder email'). "
            "The action goes into the approval queue — the user approves or rejects it."
        )

    @property
    def trust_level(self) -> str:
        return "medium"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "description": "Pocket this action belongs to",
                },
                "title": {
                    "type": "string",
                    "description": "Short action title (e.g., 'Reorder oat milk')",
                },
                "description": {
                    "type": "string",
                    "description": "Why this action is needed",
                },
                "recommendation": {
                    "type": "string",
                    "description": "What you recommend doing",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "How urgent this is",
                    "default": "medium",
                },
                "category": {
                    "type": "string",
                    "enum": ["data", "alert", "workflow", "config", "external"],
                    "description": "Category of action",
                    "default": "workflow",
                },
                "reason": {
                    "type": "string",
                    "description": "Why you're proposing this (your reasoning)",
                },
            },
            "required": ["pocket_id", "title", "recommendation"],
        }

    async def execute(
        self,
        pocket_id: str,
        title: str,
        recommendation: str,
        description: str = "",
        priority: str = "medium",
        category: str = "workflow",
        reason: str = "",
    ) -> str:
        workspace, refusal = _resolve_scope(self.name)
        if refusal:
            return refusal

        store = _get_instinct_store()
        if not store:
            return "Instinct is not available (enterprise feature)."

        try:
            from pocketpaw.instinct.models import ActionCategory, ActionPriority, ActionTrigger

            # C4-a: stamp the resolved tenant on the row. Without this the
            # Action lands NULL and every tenant's scoped pending query
            # returns it (the NULL leg of _workspace_scope's W4a filter).
            action = await store.propose(
                pocket_id=pocket_id,
                title=title,
                description=description,
                recommendation=recommendation,
                trigger=ActionTrigger(type="agent", source="pocketpaw", reason=reason or title),
                category=ActionCategory(category),
                priority=ActionPriority(priority),
                workspace_id=workspace,
            )
            return (
                f"Action proposed: '{title}' (ID: {action.id})\n"
                f"Priority: {priority} | Category: {category}\n"
                f"Recommendation: {recommendation}\n"
                f"Status: pending — waiting for human approval in the Approvals panel."
            )
        except Exception as e:
            logger.error("instinct_propose failed: %s", e)
            return f"Error proposing action: {e}"


class InstinctPendingTool(BaseTool):
    """Check pending actions awaiting approval."""

    @property
    def name(self) -> str:
        return "instinct_pending"

    @property
    def description(self) -> str:
        return (
            "Check how many actions are pending human approval, and list them. "
            "Use this to inform the user about outstanding decisions."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "description": "Filter by pocket (optional)",
                },
            },
        }

    async def execute(self, pocket_id: str | None = None) -> str:
        workspace, refusal = _resolve_scope(self.name)
        if refusal:
            return refusal

        store = _get_instinct_store()
        if not store:
            return "Instinct is not available (enterprise feature)."

        try:
            # C4-a: workspace-filtered read — never another tenant's queue.
            pending = await store.pending(pocket_id, workspace_id=workspace)
            if not pending:
                return "No pending actions — all clear."

            lines = [f"{len(pending)} action(s) pending approval:\n"]
            for a in pending:
                lines.append(f"  [{a.priority.value.upper()}] {a.title}")
                lines.append(f"    {a.recommendation}")
                lines.append(f"    ID: {a.id}")
                lines.append("")

            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"


class InstinctAuditTool(BaseTool):
    """Query the decision audit log."""

    @property
    def name(self) -> str:
        return "instinct_audit"

    @property
    def description(self) -> str:
        return (
            "Query the audit log to see recent decisions, approvals, rejections, "
            "and system events. Useful for compliance and understanding what happened."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pocket_id": {
                    "type": "string",
                    "description": "Filter by pocket (optional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max entries to return (default: 10)",
                    "default": 10,
                },
            },
        }

    async def execute(self, pocket_id: str | None = None, limit: int = 10) -> str:
        workspace, refusal = _resolve_scope(self.name)
        if refusal:
            return refusal

        store = _get_instinct_store()
        if not store:
            return "Instinct is not available (enterprise feature)."

        try:
            # C4-a: workspace-filtered read — never another tenant's trail.
            entries = await store.query_audit(
                pocket_id=pocket_id, limit=min(limit, 50), workspace_id=workspace
            )
            if not entries:
                return "No audit entries found."

            lines = [f"Recent audit entries ({len(entries)}):\n"]
            for e in entries:
                actor = e.actor.split(":")[-1] if ":" in e.actor else e.actor
                lines.append(f"  {e.event} — {e.description}")
                lines.append(f"    Actor: {actor} | Category: {e.category.value}")
                if e.ai_recommendation:
                    lines.append(f"    AI recommended: {e.ai_recommendation}")
                lines.append("")

            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"
