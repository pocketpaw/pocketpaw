# Soul tools — BaseTool implementations for soul-protocol integration.
# Created: 2026-03-02
# SoulRememberTool, SoulRecallTool, SoulEditCoreTool, SoulStatusTool,
# SoulEvaluateTool (v0.2.4+), SoulReloadTool (v0.2.4+).

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from pocketpaw.tools.protocol import BaseTool

if TYPE_CHECKING:
    from soul_protocol import Soul

    from pocketpaw.soul.manager import SoulManager


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


class SoulEvaluateTool(BaseTool):
    """Rubric-based self-evaluation of responses (v0.2.4+)."""

    def __init__(self, soul: Soul, manager: SoulManager) -> None:
        self._soul = soul
        self._manager = manager

    @property
    def name(self) -> str:
        return "soul_evaluate"

    @property
    def description(self) -> str:
        return (
            "Evaluate a response against quality rubrics. Returns heuristic scores "
            "for completeness, relevance, helpfulness, specificity, empathy, clarity, "
            "and originality. Results feed into skill XP and procedural memory."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "user_input": {
                    "type": "string",
                    "description": "The user's original message/question",
                },
                "agent_output": {
                    "type": "string",
                    "description": "The agent's response to evaluate",
                },
            },
            "required": ["user_input", "agent_output"],
        }

    async def execute(self, user_input: str = "", agent_output: str = "", **kwargs: Any) -> str:
        try:
            result = await self._manager.evaluate(user_input, agent_output)
            if result is None:
                return self._error(
                    "Self-evaluation not available. Requires soul-protocol >= 0.2.4."
                )
            return json.dumps(result, indent=2, default=str)
        except Exception as e:
            return self._error(f"Evaluation failed: {e}")


class SoulReloadTool(BaseTool):
    """Reload soul from disk (v0.2.4+)."""

    def __init__(self, manager: SoulManager) -> None:
        self._manager = manager

    @property
    def name(self) -> str:
        return "soul_reload"

    @property
    def description(self) -> str:
        return (
            "Reload the soul from its .soul file on disk. Use this when the soul "
            "file has been modified externally (e.g. by another client or tool) "
            "and you want to pick up the latest state."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        try:
            success = await self._manager.reload()
            if success:
                name = self._manager.soul.name if self._manager.soul else "unknown"
                return self._success(f"Soul reloaded successfully: {name}")
            return self._error("Reload failed. Check if the .soul file exists and is valid.")
        except Exception as e:
            return self._error(f"Reload failed: {e}")


class SoulForgetTool(BaseTool):
    """Forget memories matching a query or entity (v0.2.8+)."""

    def __init__(self, soul: Soul) -> None:
        self._soul = soul

    @property
    def name(self) -> str:
        return "soul_forget"

    @property
    def description(self) -> str:
        return (
            "Forget memories matching a query, entity, or before a date. "
            "Use for GDPR compliance, removing stale facts, or clearing sensitive data."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Content query to match for deletion"},
                "entity": {"type": "string", "description": "Entity name to forget"},
                "before_date": {"type": "string", "description": "ISO 8601 date — forget older memories"},
            },
        }

    async def execute(self, query: str = "", entity: str = "", before_date: str = "", **kw: Any) -> str:
        if not query and not entity and not before_date:
            return self._error("Provide at least one of 'query', 'entity', or 'before_date'.")
        try:
            if entity and hasattr(self._soul, "forget_entity"):
                result = await self._soul.forget_entity(entity)
            elif before_date and hasattr(self._soul, "forget_before"):
                from datetime import datetime
                result = await self._soul.forget_before(datetime.fromisoformat(before_date))
            elif query and hasattr(self._soul, "forget"):
                result = await self._soul.forget(query)
            else:
                return self._error("No valid forget operation. Provide a non-empty query, entity, or before_date.")
            total = result.get("total", "unknown") if isinstance(result, dict) else str(result)
            return self._success(f"Forgotten {total} memories. {json.dumps(result, default=str)}")
        except Exception as e:
            return self._error(f"Failed: {e}")


class SoulCoreMemoryTool(BaseTool):
    """Read the soul's core memory — persona and human description (v0.2.8+)."""

    def __init__(self, soul: Soul) -> None:
        self._soul = soul

    @property
    def name(self) -> str:
        return "soul_core_memory"

    @property
    def description(self) -> str:
        return "Read the soul's core memory (persona, human description, values)."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kw: Any) -> str:
        try:
            if not hasattr(self._soul, "get_core_memory"):
                return self._error("Requires soul-protocol >= 0.2.8.")
            cm = self._soul.get_core_memory()
            data = cm.model_dump() if hasattr(cm, "model_dump") else {"persona": getattr(cm, "persona", ""), "human": getattr(cm, "human", "")}
            return json.dumps(data, indent=2, default=str)
        except Exception as e:
            return self._error(f"Failed: {e}")


class SoulContextTool(BaseTool):
    """Get relevant soul context for a specific topic (v0.2.8+)."""

    def __init__(self, soul: Soul) -> None:
        self._soul = soul

    @property
    def name(self) -> str:
        return "soul_context"

    @property
    def description(self) -> str:
        return "Get relevant soul context for a topic — returns state, memories, and self-model insights."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Topic to get context for"},
                "max_memories": {"type": "integer", "description": "Max memories (default: 5)", "default": 5},
            },
            "required": ["prompt"],
        }

    async def execute(self, prompt: str, max_memories: int = 5, **kw: Any) -> str:
        try:
            if not hasattr(self._soul, "context_for"):
                return self._error("Requires soul-protocol >= 0.2.8.")
            context = await self._soul.context_for(prompt, max_memories=max_memories)
            return context if context else "No relevant context found."
        except Exception as e:
            return self._error(f"Failed: {e}")
