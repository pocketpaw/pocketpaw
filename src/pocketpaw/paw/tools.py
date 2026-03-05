# Soul tools — four BaseTool implementations for soul-protocol integration.
# Created: 2026-03-02
# SoulRememberTool, SoulRecallTool, SoulEditCoreTool, SoulStatusTool.

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pocketpaw.tools.protocol import BaseTool

if TYPE_CHECKING:
    from soul_protocol import Soul


class SoulRememberTool(BaseTool):
    """Store memories via soul.remember()."""

    def __init__(self, soul: Soul) -> None:
        self._soul = soul

    @property
    def name(self) -> str:
        return "soul_remember"

    @property
    def description(self) -> str:
        return (
            "Store a memory in the soul's persistent memory. Use this to remember "
            "facts about the project, user preferences, or important context that "
            "should persist across sessions."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The information to remember (be specific and clear)",
                },
                "importance": {
                    "type": "integer",
                    "description": "Importance level from 1 (trivial) to 10 (critical). Default: 5",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["content"],
        }

    async def execute(self, content: str, importance: int = 5, **kwargs: Any) -> str:
        try:
            await self._soul.remember(content, importance=importance)
            return self._success(
                f"Remembered (importance={importance}): "
                f"{content[:100]}{'...' if len(content) > 100 else ''}"
            )
        except Exception as e:
            return self._error(f"Failed to store memory: {e}")


class SoulRecallTool(BaseTool):
    """Search memories via soul.recall()."""

    def __init__(self, soul: Soul) -> None:
        self._soul = soul

    @property
    def name(self) -> str:
        return "soul_recall"

    @property
    def description(self) -> str:
        return (
            "Search the soul's persistent memories. Returns relevant memories "
            "matching the query, ordered by relevance."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for in memories",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of memories to return (default: 5)",
                    "minimum": 1,
                    "maximum": 20,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, limit: int = 5, **kwargs: Any) -> str:
        try:
            memories = await self._soul.recall(query, limit=limit)
            if not memories:
                return f"No memories found matching: {query}"

            lines = [f"Found {len(memories)} memories:\n"]
            for i, m in enumerate(memories, 1):
                emotion = f" ({m.emotion})" if hasattr(m, "emotion") and m.emotion else ""
                lines.append(f"{i}. [{m.importance}] {m.content[:200]}{emotion}")

            return "\n".join(lines)
        except Exception as e:
            return self._error(f"Failed to search memories: {e}")


class SoulEditCoreTool(BaseTool):
    """Edit persona/human core memory via soul.edit_core_memory()."""

    def __init__(self, soul: Soul) -> None:
        self._soul = soul

    @property
    def name(self) -> str:
        return "soul_edit_core"

    @property
    def description(self) -> str:
        return (
            "Edit the soul's core memory — the persistent persona and human descriptions. "
            "Use this to update who the agent is (persona) or who the user is (human)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "persona": {
                    "type": "string",
                    "description": "Updated persona description for the agent",
                },
                "human": {
                    "type": "string",
                    "description": "Updated description of the human user",
                },
            },
            "required": [],
        }

    async def execute(
        self, persona: str | None = None, human: str | None = None, **kwargs: Any
    ) -> str:
        if not persona and not human:
            return self._error("Provide at least one of 'persona' or 'human' to edit.")

        try:
            edit_args: dict[str, str] = {}
            if persona:
                edit_args["persona"] = persona
            if human:
                edit_args["human"] = human

            await self._soul.edit_core_memory(**edit_args)

            updated = ", ".join(f"{k}" for k in edit_args)
            return self._success(f"Core memory updated: {updated}")
        except Exception as e:
            return self._error(f"Failed to edit core memory: {e}")


class SoulStatusTool(BaseTool):
    """Check soul state, mood, energy, and active domains."""

    def __init__(self, soul: Soul) -> None:
        self._soul = soul

    @property
    def name(self) -> str:
        return "soul_status"

    @property
    def description(self) -> str:
        return (
            "Check the soul's current state including mood, energy level, "
            "social battery, and active knowledge domains."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self, **kwargs: Any) -> str:
        try:
            state = self._soul.state
            status: dict[str, Any] = {}

            if hasattr(state, "mood"):
                status["mood"] = state.mood
            if hasattr(state, "energy"):
                status["energy"] = state.energy
            if hasattr(state, "social_battery"):
                status["social_battery"] = state.social_battery

            # Active self-image domains
            if hasattr(self._soul, "self_model") and self._soul.self_model:
                try:
                    images = self._soul.self_model.get_active_self_images(limit=5)
                    status["domains"] = [
                        {"domain": img.domain, "confidence": img.confidence} for img in images
                    ]
                except Exception:
                    pass

            if not status:
                return "Soul state: active (no detailed state available)"

            return json.dumps(status, indent=2, default=str)
        except Exception as e:
            return self._error(f"Failed to get soul status: {e}")
