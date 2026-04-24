---
{
  "title": "CLI Memory Command: Inspecting and Searching Long-Term Agent Memories",
  "summary": "The `memory` CLI command provides operators with visibility into PocketPaw's persistent memory system — showing statistics about stored memories, daily notes, and sessions, or performing keyword searches across the memory store. It bridges the gap between the agent's internal memory manager and human-readable inspection.",
  "concepts": [
    "memory manager",
    "long-term memory",
    "MEMORY.md",
    "daily notes",
    "sessions",
    "async CLI",
    "memory search",
    "asyncio.run",
    "memory stats",
    "vector retrieval"
  ],
  "categories": [
    "CLI",
    "Memory System"
  ],
  "source_docs": [
    "c1875db7865e019b"
  ],
  "backlinks": null,
  "word_count": 451,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/cli/memory.py` implements the `pocketpaw memory` subcommand. PocketPaw maintains a multi-tier memory system that persists facts, session summaries, and daily notes across restarts. This command gives operators a way to inspect and search that data without reading raw files.

## Async Bridge via `asyncio.run`

The memory manager is an async interface. The CLI is a synchronous entry point. Rather than making `run_memory_cmd` async (which would require the caller to manage an event loop), each async sub-function is wrapped with `asyncio.run(...)` at the dispatch point:

```python
return asyncio.run(_memory_stats(as_json))
return asyncio.run(_search_memories(query, limit, as_json))
```

This pattern creates a new event loop per invocation, which is acceptable for CLI commands that run once and exit.

## Memory Stats: `_memory_stats`

The stats function inspects the file system directly rather than going through the memory manager API. It checks for:

- **`MEMORY.md`**: The long-term memory file. The count of `## ` header occurrences approximates the number of stored memory entries, since each entry is typically a second-level Markdown heading.
- **Daily note files** (`YYYY-MM-DD.md`): Globbed by date pattern.
- **Session files** (`memory/sessions/*.json`): Counted by glob.

Going directly to the file system means stats are available even if the memory manager fails to initialize. The trade-off is that the counts are approximations — a corrupted MEMORY.md with misformatted headers would undercount entries.

## Memory Search: `_search_memories`

Search delegates entirely to `mm.search(query, limit=limit)`, where `mm` is the full memory manager instance. This gives access to whatever search capability the backend supports (keyword matching, vector similarity, or hybrid). Results are rendered as a compact list with header, tags, and truncated content:

```python
if len(content) > 120:
    content = content[:117] + "..."
```

The 120-character truncation prevents long memory entries from overwhelming the terminal. Tags are prefixed with `#` to make them visually distinct from content.

## JSON Output

Both stats and search support `--json`. For search results, the JSON output includes the memory entry's `id`, `content`, `tags`, `type`, and `header` metadata field. The `type` field handles both enum and string representations:

```python
"type": e.type.value if hasattr(e.type, "value") else str(e.type),
```

This guard exists because memory entries from different backend versions may represent `type` as an enum with a `.value` attribute or as a plain string.

## Known Gaps

- **Stats use file system heuristics**: The `##` count in MEMORY.md is an approximation. Memories that span multiple `##` headings, or headings used for other purposes, would skew the count.
- **No delete or edit operations**: The CLI supports read-only access to memories. Deleting or correcting a specific memory entry requires direct file editing or using the sessions interface.
- **No pagination in search**: The `limit` parameter caps results but there is no `--offset` for paginating through large result sets.
