---
{
  "title": "Memory Tools: Remember, Recall, and Forget Long-Term Agent Memory",
  "summary": "The `memory.py` module provides three `BaseTool` subclasses — `RememberTool`, `RecallTool`, and `ForgetTool` — that expose PocketPaw's long-term memory manager to the agent, enabling it to save facts about the user, retrieve them in future sessions, and delete outdated or incorrect memories. These tools are the agent-facing surface of the memory manager, which persists memories across session boundaries independently of the LLM's context window.",
  "concepts": [
    "RememberTool",
    "RecallTool",
    "ForgetTool",
    "memory manager",
    "long-term memory",
    "semantic search",
    "tags",
    "session persistence",
    "BaseTool",
    "get_memory_manager"
  ],
  "categories": [
    "builtin tools",
    "memory",
    "persistence",
    "agent capabilities"
  ],
  "source_docs": [
    "f4171beeb3da28a4"
  ],
  "backlinks": null,
  "word_count": 590,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`memory.py` was created 2026-02-05 as part of the Memory System Enhancement. The motivation is straightforward: without persistent memory, every new session starts from zero. The agent cannot remember that the user prefers metric units, that their company uses a specific invoice format, or that a particular project name refers to a confidential initiative. The three tools in this module give the agent explicit control over what it remembers and what it forgets.

## RememberTool

Tool name: `remember`. Saves a piece of information to long-term memory with optional tags for categorization. The docstring instructs the agent on when to use this tool:

```
"Save important information to long-term memory. Use this to remember facts
about the user, their preferences, project details, or anything they want
you to remember for future conversations."
```

The `content` parameter description instructs the agent to be "specific and clear" — vague memories like "user likes reports" are less useful than "user prefers PDF reports with the company logo in the top-left corner."

The `tags` parameter is optional but enables structured retrieval. A memory tagged `["invoice", "format"]` can be recalled by tag without requiring a semantic search query.

## RecallTool

Tool name: `recall`. Queries long-term memory using a natural-language query and returns the `limit` most relevant memories (default value specified in the memory manager). The search is semantic — the memory manager uses the query to find relevant stored content even if the exact words do not match.

The docstring emphasizes proactive use:

```
"Use this tool to recall previously saved information about the user before
starting tasks that might benefit from context."
```

This instruction pushes the agent to recall at the start of relevant tasks rather than only when explicitly asked, making memory a proactive resource rather than a reactive one.

## ForgetTool

Tool name: `forget`. Deletes memories matching a query. This is important for privacy and correctness: if the user's preferences change or they provide incorrect information, the agent needs a way to remove stale memories rather than accumulating conflicting facts.

The `execute` method takes a `query` string rather than a memory ID, which means deletion is search-based. The memory manager finds all memories matching the query and removes them. This is powerful but potentially lossy — a broad query could delete more than intended.

## Memory manager delegation

All three tools delegate to `get_memory_manager()` from `pocketpaw.memory.manager`, which is imported at module level (unlike the lazy imports in the Fabric and Instinct tools). The memory manager is a core PocketPaw component rather than an enterprise add-on, so there is no risk of import failure.

```python
from pocketpaw.memory.manager import get_memory_manager

class RememberTool(BaseTool):
    async def execute(self, content: str, tags: list[str] | None = None) -> str:
        manager = get_memory_manager()
        # delegate to manager
```

## No trust level override

None of the three tools override `trust_level`, meaning they inherit the default trust level from `BaseTool`. Memory is a core agent capability rather than a privileged operation — the agent should be able to remember and recall without elevated permissions.

## Known Gaps

- **Search-based delete is lossy**: `ForgetTool` deletes all memories matching a query. There is no dry-run mode to preview what would be deleted, and there is no undo.
- **No memory expiry**: Memories do not have a TTL (time-to-live) or expiry mechanism. Stale facts accumulate until explicitly forgotten.
- **No memory listing**: There is no `ListMemoriesTool` to browse all stored memories. The agent can only access memories through semantic search, which may miss relevant memories if the query does not match well.