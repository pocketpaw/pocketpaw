# Session management tools — let the agent manage chat sessions via tool calls.
# Created: 2026-02-12
#
# These tools replace the old regex-based NL detection. The LLM decides when
# to invoke them based on natural language, which handles any phrasing.

from __future__ import annotations

import uuid
from typing import Any

from pocketclaw.memory.manager import get_memory_manager
from pocketclaw.tools.protocol import BaseTool


class NewSessionTool(BaseTool):
    """Start a fresh conversation session."""

    @property
    def name(self) -> str:
        return "new_session"

    @property
    def description(self) -> str:
        return (
            "Start a fresh conversation session. The previous session is preserved "
            "and can be resumed later. Call this when the user wants to start over "
            "or begin a new topic."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": "The current session key (provided in system prompt).",
                },
            },
            "required": ["session_key"],
        }

    async def execute(self, session_key: str) -> str:
        try:
            memory = get_memory_manager()
            new_key = f"{session_key}:{uuid.uuid4().hex[:8]}"
            await memory.set_session_alias(session_key, new_key)
            return (
                "Started a new conversation session. "
                "Previous sessions are preserved — use list_sessions to see them."
            )
        except Exception as e:
            return self._error(f"Failed to create new session: {e}")


class ListSessionsTool(BaseTool):
    """List all conversation sessions for this chat."""

    @property
    def name(self) -> str:
        return "list_sessions"

    @property
    def description(self) -> str:
        return (
            "List all conversation sessions for the current chat. Returns session "
            "titles, message counts, and which one is active."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": "The current session key (provided in system prompt).",
                },
            },
            "required": ["session_key"],
        }

    async def execute(self, session_key: str) -> str:
        try:
            memory = get_memory_manager()
            sessions = await memory.list_sessions_for_chat(session_key)

            if not sessions:
                return "No sessions found. Start chatting to create one!"

            lines = []
            for i, s in enumerate(sessions, 1):
                marker = " (active)" if s["is_active"] else ""
                title = s["title"] or "New Chat"
                count = s["message_count"]
                lines.append(f"{i}. {title} ({count} msgs){marker}")

            return "\n".join(lines)
        except Exception as e:
            return self._error(f"Failed to list sessions: {e}")


class SwitchSessionTool(BaseTool):
    """Switch to a different conversation session."""

    @property
    def name(self) -> str:
        return "switch_session"

    @property
    def description(self) -> str:
        return (
            "Switch to a different conversation session by number (from list_sessions) "
            "or by searching session titles. Use this when the user wants to resume "
            "or go back to a previous conversation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": "The current session key (provided in system prompt).",
                },
                "target": {
                    "type": "string",
                    "description": (
                        "Session number (from list_sessions) or text to search in session titles."
                    ),
                },
            },
            "required": ["session_key", "target"],
        }

    async def execute(self, session_key: str, target: str) -> str:
        try:
            memory = get_memory_manager()
            sessions = await memory.list_sessions_for_chat(session_key)

            if not sessions:
                return "No sessions found."

            # Try numeric reference
            if target.strip().isdigit():
                n = int(target.strip())
                if n < 1 or n > len(sessions):
                    return f"Invalid session number. Choose 1-{len(sessions)}."
                chosen = sessions[n - 1]
                await memory.set_session_alias(session_key, chosen["session_key"])
                return f"Switched to session: {chosen['title'] or 'New Chat'}"

            # Text search
            query_lower = target.lower()
            matches = [
                s
                for s in sessions
                if query_lower in s["title"].lower() or query_lower in s["preview"].lower()
            ]

            if not matches:
                return f'No sessions matching "{target}".'

            if len(matches) == 1:
                chosen = matches[0]
                await memory.set_session_alias(session_key, chosen["session_key"])
                return f"Switched to session: {chosen['title'] or 'New Chat'}"

            # Multiple matches
            lines = [f'Multiple sessions match "{target}":']
            for i, s in enumerate(matches, 1):
                marker = " (active)" if s["is_active"] else ""
                lines.append(f"{i}. {s['title'] or 'New Chat'} ({s['message_count']} msgs){marker}")
            lines.append("Please specify which one by number.")
            return "\n".join(lines)
        except Exception as e:
            return self._error(f"Failed to switch session: {e}")


class ClearSessionTool(BaseTool):
    """Clear the current session's conversation history."""

    @property
    def name(self) -> str:
        return "clear_session"

    @property
    def description(self) -> str:
        return (
            "Clear all messages from the current conversation session. "
            "The session itself is kept but its history is wiped."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": "The current session key (provided in system prompt).",
                },
            },
            "required": ["session_key"],
        }

    async def execute(self, session_key: str) -> str:
        try:
            memory = get_memory_manager()
            resolved = await memory.resolve_session_key(session_key)
            count = await memory.clear_session(resolved)
            if count:
                return f"Cleared {count} messages from the current session."
            return "Session is already empty."
        except Exception as e:
            return self._error(f"Failed to clear session: {e}")


class RenameSessionTool(BaseTool):
    """Rename the current conversation session."""

    @property
    def name(self) -> str:
        return "rename_session"

    @property
    def description(self) -> str:
        return "Rename the current conversation session to a new title."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": "The current session key (provided in system prompt).",
                },
                "title": {
                    "type": "string",
                    "description": "The new title for the session.",
                },
            },
            "required": ["session_key", "title"],
        }

    async def execute(self, session_key: str, title: str) -> str:
        try:
            memory = get_memory_manager()
            resolved = await memory.resolve_session_key(session_key)
            ok = await memory.update_session_title(resolved, title)
            if ok:
                return f'Session renamed to "{title}".'
            return "Could not rename — session not found in index."
        except Exception as e:
            return self._error(f"Failed to rename session: {e}")


class DeleteSessionTool(BaseTool):
    """Delete the current conversation session."""

    @property
    def name(self) -> str:
        return "delete_session"

    @property
    def description(self) -> str:
        return (
            "Permanently delete the current conversation session and all its messages. "
            "The next message will start a fresh conversation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "session_key": {
                    "type": "string",
                    "description": "The current session key (provided in system prompt).",
                },
            },
            "required": ["session_key"],
        }

    async def execute(self, session_key: str) -> str:
        try:
            memory = get_memory_manager()
            resolved = await memory.resolve_session_key(session_key)
            deleted = await memory.delete_session(resolved)
            if hasattr(memory._store, "remove_session_alias"):
                await memory._store.remove_session_alias(session_key)
            if deleted:
                return "Session deleted. Your next message will start a fresh conversation."
            return "No session to delete."
        except Exception as e:
            return self._error(f"Failed to delete session: {e}")
