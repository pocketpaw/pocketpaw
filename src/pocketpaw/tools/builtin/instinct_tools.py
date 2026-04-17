# Instinct tools — agent tools for the decision pipeline.
# Created: 2026-03-28 — Lets the agent propose actions, check pending, read audit.

import logging
from typing import Any

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


def _get_instinct_store():
    """Lazy import to avoid circular deps and missing ee/ module."""
    try:
        from ee.api import get_instinct_store

        return get_instinct_store()
    except ImportError:
        return None


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
        store = _get_instinct_store()
        if not store:
            return "Instinct is not available (enterprise feature)."

        try:
            from ee.instinct.models import ActionCategory, ActionPriority, ActionTrigger

            action = await store.propose(
                pocket_id=pocket_id,
                title=title,
                description=description,
                recommendation=recommendation,
                trigger=ActionTrigger(type="agent", source="pocketpaw", reason=reason or title),
                category=ActionCategory(category),
                priority=ActionPriority(priority),
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
        store = _get_instinct_store()
        if not store:
            return "Instinct is not available (enterprise feature)."

        try:
            pending = await store.pending(pocket_id)
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
        store = _get_instinct_store()
        if not store:
            return "Instinct is not available (enterprise feature)."

        try:
            entries = await store.query_audit(pocket_id=pocket_id, limit=min(limit, 50))
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
