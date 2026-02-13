# Memory tools - allow agent to save/recall long-term memories.
# Created: 2026-02-05
# Part of Memory System Enhancement

from typing import Any

from pocketclaw.memory.manager import get_memory_manager
from pocketclaw.tools.protocol import BaseTool


class RememberTool(BaseTool):
    """Save information to long-term memory.

    Use this tool to remember facts, preferences, or important information
    about the user that should persist across sessions.
    """

    @property
    def name(self) -> str:
        return "remember"

    @property
    def description(self) -> str:
        return (
            "Save important information to long-term memory. Use this to remember "
            "facts about the user, their preferences, project details, or anything "
            "they want you to remember for future conversations."
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
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional tags to categorize the memory (e.g., 'preference', 'project', 'personal')",
                },
            },
            "required": ["content"],
        }

    async def execute(self, content: str, tags: list[str] | None = None) -> str:
        """Save to long-term memory."""
        try:
            manager = get_memory_manager()
            entry_id = await manager.remember(content, tags=tags or [])

            tags_str = f" with tags: {', '.join(tags)}" if tags else ""
            return f"✅ Remembered{tags_str}: {content[:100]}{'...' if len(content) > 100 else ''}"

        except Exception as e:
            return self._error(f"Failed to save memory: {str(e)}")


class RecallTool(BaseTool):
    """Search long-term memories.

    Use this tool to recall previously saved information about the user
    or search for specific memories.
    """

    @property
    def name(self) -> str:
        return "recall"

    @property
    def description(self) -> str:
        return (
            "Search long-term memories. Use this to recall previously saved "
            "information about the user, their preferences, or project details."
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
                    "default": 5,
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str, limit: int = 5) -> str:
        """Search memories."""
        try:
            manager = get_memory_manager()
            results = await manager.search(query, limit=limit)

            if not results:
                return f"No memories found matching: {query}"

            lines = [f"Found {len(results)} memories:\n"]
            for i, entry in enumerate(results, 1):
                tags_str = f" [{', '.join(entry.tags)}]" if entry.tags else ""
                lines.append(f"{i}. {entry.content[:200]}{tags_str}")

            return "\n".join(lines)

        except Exception as e:
            return self._error(f"Failed to search memories: {str(e)}")


class ForgetTool(BaseTool):
    """Remove information from long-term memory.

    Use this tool to delete specific memories that are no longer accurate
    or that the user wants removed.
    """

    @property
    def name(self) -> str:
        return "forget"

    @property
    def description(self) -> str:
        return (
            "Remove information from long-term memory. Searches for matching memories "
            "and deletes them. Use when the user wants to correct or remove stored facts."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What to search for and forget",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: str) -> str:
        """Search and delete matching memories."""
        try:
            manager = get_memory_manager()
            results = await manager.search(query, limit=5)

            if not results:
                return f"No memories found matching: {query}"

            deleted = 0
            for entry in results:
                ok = await manager._store.delete(entry.id)
                if ok:
                    deleted += 1

            return f"Forgot {deleted} memory(ies) matching: {query}"

        except Exception as e:
            return self._error(f"Failed to forget memory: {str(e)}")
