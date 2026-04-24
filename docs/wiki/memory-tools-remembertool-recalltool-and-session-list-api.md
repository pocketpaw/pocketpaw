---
{
  "title": "Memory Tools: RememberTool, RecallTool, and Session List API",
  "summary": "Tests for the agent-facing memory tools (`remember` and `recall`) and the session list REST endpoint. Covers tool schema validation, content persistence, tag filtering, truncation behavior, and the API's ability to enumerate stored sessions.",
  "concepts": [
    "RememberTool",
    "RecallTool",
    "memory tools",
    "tool schema",
    "tag filtering",
    "truncation",
    "session list API",
    "MemoryManager",
    "content persistence",
    "agent tools",
    "built-in tools"
  ],
  "categories": [
    "memory system",
    "agent tools",
    "testing",
    "API",
    "test"
  ],
  "source_docs": [
    "4cf685b379553ed9"
  ],
  "backlinks": null,
  "word_count": 522,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw exposes memory to agents through two built-in tools: `RememberTool` (save a fact) and `RecallTool` (search for facts). This test file validates both tools end-to-end and also covers a session list API endpoint that allows external dashboards to browse stored conversations.

## RememberTool (`TestRememberTool`)

`RememberTool` lets an agent save a piece of information permanently under the user's memory store. Tests cover:

- **Schema validation** (`test_tool_definition`, `test_definition_formats`): The tool must declare the correct `name`, `description`, and parameter schema. This is not cosmetic — the schema is what gets sent to the LLM, so a wrong schema causes the model to call the tool with wrong argument names.
- **Content persistence** (`test_remember_persists`): After calling `remember`, a subsequent call to the underlying `MemoryManager` must return the saved entry. Without this, memories appear to save but silently drop.
- **Tag support** (`test_remember_with_tags`): Tags are attached to entries at save time and must round-trip correctly so recall can filter by tag.
- **Empty content** (`test_remember_empty_content`): Saving an empty string should not crash — the tool must handle this gracefully (likely returning an error message to the agent rather than raising).
- **Truncation** (`test_remember_long_content_truncated_in_response`): Very long memories are saved in full but the tool's response to the LLM is truncated. This prevents the response from consuming the entire context window in a confirmation message.

## RecallTool (`TestRecallTool`)

`RecallTool` searches the memory store and returns matching entries to the agent.

- **No results** (`test_recall_no_results`): When nothing matches, the tool returns a clear "no memories found" message rather than an empty list or an error, because the LLM needs human-readable feedback.
- **Results with limit** (`test_recall_with_limit`): The tool respects a `limit` parameter to cap results, preventing context bloat when the store is large.
- **Tag display** (`test_recall_shows_tags`): Returned memories include their tags in the output so the agent can reason about provenance.

## Integration Flow (`TestMemoryToolsIntegration`)

End-to-end tests simulate an agent calling `remember` then `recall`:

- `test_remember_then_recall_workflow`: A single save followed by a search returns the saved item. This confirms the tools interact correctly through the same `MemoryManager` instance rather than working against separate stores.
- `test_multiple_memories_recall`: Multiple saves, then a search, returns all relevant items. Verifies the store accumulates rather than overwrites.

## Session List API (`TestSessionListAPI`)

The session list endpoint lets the dashboard show users a history of conversations stored on disk. Tests cover:

- `test_list_sessions`: Confirms that session directories are enumerated and returned in a structured format.
- `test_list_sessions_empty_directory`: Returns an empty list (not an error) when no sessions exist yet.
- `test_list_sessions_malformed`: Malformed session files or directories do not crash the endpoint — they are skipped silently. This matters because users may manually browse and accidentally corrupt a session file.

## Fixture Design

The `mock_memory_manager` fixture patches `get_memory_manager` at the module level rather than constructing a mock from scratch. This ensures the tool under test uses the same dependency-injection path as production code, so no import-level caching issues are missed during tests.

## Known Gaps

No explicit TODOs in this file. The suite does not test concurrent `remember` calls — a potential race if two agents share the same `MemoryManager` instance and write simultaneously.
