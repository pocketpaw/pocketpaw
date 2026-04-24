---
{
  "title": "CLI Sessions Command: Listing, Searching, and Deleting Chat Sessions",
  "summary": "The `sessions` command provides operators with management access to PocketPaw's persisted chat sessions — listing recent sessions with metadata, searching session content by keyword, and deleting sessions by key. It wraps the async memory manager's session interface with a synchronous CLI dispatcher.",
  "concepts": [
    "chat sessions",
    "session management",
    "memory manager",
    "async CLI",
    "asyncio.run",
    "session search",
    "session delete",
    "list sessions",
    "single-user mode"
  ],
  "categories": [
    "CLI",
    "Memory System"
  ],
  "source_docs": [
    "7efc0fd007c25fd1"
  ],
  "backlinks": null,
  "word_count": 507,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/cli/sessions.py` implements the `pocketpaw sessions` subcommand. Chat sessions in PocketPaw are persisted records of conversation turns, stored and indexed by the memory manager. This command exposes the three most common management operations: list, search, and delete.

## Async Dispatch via `_run`

A small helper function named `_run` encapsulates the `asyncio.run` pattern:

```python
def _run(coro) -> int:
    return asyncio.run(coro)
```

This is identical in purpose to the pattern used in `memory.py`, but extracted into a named function rather than inlined. The named helper makes the dispatch logic in `run_sessions_cmd` readable — `_run(_list_sessions(...))`, `_run(_delete_session(...))`, etc. — without requiring the caller to know about the async boundary.

## Listing Sessions

`_list_sessions` calls `mm.list_sessions_for_chat("default")` to retrieve all sessions. The `"default"` parameter reflects a design choice: PocketPaw currently operates in single-user mode where all sessions belong to one implicit user context. The result is sliced to `limit` before rendering.

The table format includes title, message count, and last activity timestamp. Active sessions are marked with a green `*` marker:

```python
marker = f" {GREEN}*{RESET}" if is_active else ""
```

This is useful when PocketPaw is running and a session is currently in use — operators can see which session is live before deciding to delete another.

## Deleting Sessions

`_delete_session` returns a boolean from the memory manager indicating whether the key was found and deleted. If `False`, a failure message is shown and exit code 1 is returned. This explicit boolean check prevents silent no-ops: deleting a session with a typo in the key will fail loudly rather than succeeding vacuously.

## Searching Sessions

`_search_sessions` calls `mm.search_sessions(query, limit=limit)`, which searches across session content (individual message turns). Results are grouped by session key, with up to three matching message excerpts shown per session:

```python
for m in matches[:3]:
    role = m.get("role", "")
    content = m.get("content", "")[:80]
```

The three-match limit and 80-character truncation are display constraints — the full search results are available via `--json`. Showing role (`user` / `assistant`) alongside the excerpt lets operators quickly determine whether a keyword appeared in their own messages or in the agent's responses.

## JSON Output

All three operations support `--json`. For listing, the raw session objects from the memory manager are output directly. For search, the result structure (session key + matches) is output without transformation. For delete, the success/failure is communicated via exit code rather than JSON body, which is appropriate since delete produces no meaningful data on success.

## Known Gaps

- **`list_sessions_for_chat("default")` hardcodes user context**: In a future multi-user deployment, this would need a `--user` flag to filter sessions by owner. The `"default"` string is a placeholder for single-user mode.
- **No bulk delete**: There is no `sessions delete-all` or `sessions delete --before <date>` command. Operators who need to clean up large numbers of old sessions must script individual deletes or manipulate the storage layer directly.
- **Search result ordering is backend-dependent**: The CLI does not sort or re-rank search results. The ordering depends entirely on the memory manager's search implementation, which may vary between the file backend and mem0.
