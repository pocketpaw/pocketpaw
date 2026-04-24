---
{
  "title": "Session Management Tools: LLM-Driven Conversation Branching",
  "summary": "Six `BaseTool` subclasses expose session lifecycle operations — create, list, switch, clear, rename, delete — as agent-callable tools, replacing an earlier regex-based NL detection approach. The LLM decides when to invoke them based on conversational context, making session control robust to any phrasing.",
  "concepts": [
    "NewSessionTool",
    "ListSessionsTool",
    "SwitchSessionTool",
    "ClearSessionTool",
    "RenameSessionTool",
    "DeleteSessionTool",
    "session_key",
    "memory_manager",
    "conversation_branching",
    "BaseTool"
  ],
  "categories": [
    "tools",
    "session-management",
    "memory",
    "conversation"
  ],
  "source_docs": [
    "4a92c7f93c3fe53a"
  ],
  "backlinks": null,
  "word_count": 489,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`sessions.py` (created 2026-02-12) ships six tools that manage named conversation sessions within a single chat. The design comment is revealing: these tools "replace the old regex-based NL detection." The previous approach tried to detect user intent by pattern-matching phrases like "start over" or "new topic" with regexes, which broke on slight phrasing variations. Moving to LLM-invoked tools means the model's own language understanding handles intent detection — far more robust and maintainable.

## Architecture: Session Keys

Every tool accepts a `session_key` parameter, described as "provided in system prompt." This is the session identity injected by the agent loop at conversation start. Tools derive new session identifiers from it rather than generating fully random keys:

```python
new_key = f"{session_key}:{uuid.uuid4().hex[:8]}"
```

The parent-key prefix preserves the chat lineage — all sessions for a given chat share a common prefix, which makes listing and switching operations scoped correctly to the current user's chat. This is important in multi-tenant deployments where multiple users' session keys must not collide.

## NewSessionTool

```python
class NewSessionTool(BaseTool):
    async def execute(self, session_key: str) -> str:
        memory = get_memory_manager()
        new_key = f"{session_key}:{uuid.uuid4().hex[:8]}"
        await memory.set_session_alias(session_key, new_key)
```

Creating a new session doesn't delete the previous one — it creates a new key and updates the active alias. Old sessions remain accessible via `list_sessions` and `switch_session`. This non-destructive design prevents accidental data loss when users say "let's start over" but might later want to resume the previous conversation.

## ListSessionsTool and SwitchSessionTool

`list_sessions` returns session titles, message counts, and the active indicator. `switch_session` accepts a `target` parameter (session key or title) to reactivate a previous session. Together they give agents the primitives needed to implement a session picker UI or respond to "go back to our earlier conversation about X."

## ClearSessionTool

Clears the current session's message history without deleting the session record itself. The distinction matters: `delete_session` removes the session entirely, while `clear_session` keeps the session named and accessible but wipes its messages. This mirrors how most chat applications handle "clear chat" vs. "delete chat."

## RenameSessionTool and DeleteSessionTool

`rename_session` accepts a `title` string and updates the session's human-readable label, enabling agents to auto-name sessions based on their topic (e.g., "Renamed session to 'Tax Questions 2026'"). `delete_session` performs irreversible removal — there is no confirmation gate in the tool itself; the LLM is expected to confirm intent with the user before invoking it.

## Error Handling

All six tools follow the same pattern: a single `try/except Exception` block that calls `self._error()` on failure. Memory manager errors (connection issues, serialization failures) are caught and returned as formatted error strings rather than exceptions, keeping the agent loop stable.

## Known Gaps

- `SwitchSessionTool` and `DeleteSessionTool` source code was not fully shown; the exact implementation of `target` resolution (by key vs. title) is opaque.
- There is no locking mechanism — concurrent invocations of `new_session` or `switch_session` could race in multi-agent scenarios.
- No audit log of session operations is maintained.
