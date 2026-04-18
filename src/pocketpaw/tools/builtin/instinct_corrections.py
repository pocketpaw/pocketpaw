# Instinct corrections tool — agent-facing surface for learned human edits.
# Created: 2026-04-12 (Move 1 PR-B) — Lets an agent fetch recent corrections for
# a pocket before proposing its next action, so past human edits shape the draft.
# Pairs with the correction_soul_bridge, which also feeds the same signal into
# soul-protocol's automatic memory injection when a soul is loaded.

from __future__ import annotations

import logging
from typing import Any

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


def _get_instinct_store():
    """Lazy import — degrades gracefully when ee/ is not installed."""
    try:
        from ee.api import get_instinct_store

        return get_instinct_store()
    except ImportError:
        return None


class InstinctCorrectionsTool(BaseTool):
    """Fetch recent human corrections on actions within a pocket."""

    @property
    def name(self) -> str:
        return "instinct_corrections"

    @property
    def description(self) -> str:
        return (
            "Fetch recent human edits (corrections) applied to previously proposed "
            "actions in a pocket. Use this BEFORE proposing a new action so your "
            "draft already matches the style and thresholds the user prefers — e.g. "
            "if they consistently edit the greeting tone or cap a discount percentage, "
            "match that pattern in the new proposal."
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
                    "description": "Pocket whose corrections you want to review",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max corrections to return (default: 10, max: 50)",
                    "default": 10,
                },
            },
            "required": ["pocket_id"],
        }

    async def execute(self, pocket_id: str, limit: int = 10) -> str:
        store = _get_instinct_store()
        if not store:
            return "Instinct is not available (enterprise feature)."

        try:
            corrections = await store.get_corrections_for_pocket(
                pocket_id=pocket_id,
                limit=min(max(limit, 1), 50),
            )
        except Exception as exc:
            logger.error("instinct_corrections lookup failed: %s", exc)
            return f"Error loading corrections: {exc}"

        if not corrections:
            return (
                f"No corrections captured yet for pocket {pocket_id}. "
                "Propose freely — nothing to align with."
            )

        lines = [
            f"{len(corrections)} recent correction(s) for pocket {pocket_id}:",
            "",
        ]
        for c in corrections:
            lines.append(f"- {c.action_title} (edited by {c.actor})")
            lines.append(f"    Summary: {c.context_summary}")
            for patch in c.patches[:5]:
                before = _fmt(patch.before)
                after = _fmt(patch.after)
                lines.append(f"    {patch.path}: {before} → {after}")
            if len(c.patches) > 5:
                lines.append(f"    (+{len(c.patches) - 5} more field changes)")
            lines.append("")

        lines.append(
            "When proposing your next action, pre-apply these patterns unless the "
            "situation clearly calls for something different.",
        )
        return "\n".join(lines)


def _fmt(value: object) -> str:
    if value is None:
        return "(none)"
    s = str(value)
    return s if len(s) <= 60 else s[:57] + "..."
