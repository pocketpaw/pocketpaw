---
{
  "title": "Workspace Message Search Tests",
  "summary": "This test module covers MessageService's workspace-wide message search, verifying that results respect group visibility (channels vs. private rooms), workspace scope isolation, regex-safety of user queries, and server-side result limits. Tests run against an in-memory MongoDB via the shared beanie_memory_db fixture to exercise real Mongo query behavior.",
  "concepts": [
    "MessageService",
    "workspace search",
    "privacy isolation",
    "regex escaping",
    "result capping",
    "beanie_memory_db",
    "mongomock",
    "multi-tenant",
    "channel visibility",
    "private room access control"
  ],
  "categories": [
    "testing",
    "search",
    "security",
    "chat",
    "test"
  ],
  "source_docs": [
    "tests/cloud/chat/test_message_search.py"
  ],
  "backlinks": null,
  "word_count": 510,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_message_search.py` covers the workspace-wide search feature added in Cluster E sub-PR 2. Rather than mocking the database layer, these tests use the `beanie_memory_db` fixture (mongomock-motor) so the actual MongoDB aggregation pipeline and regex queries are exercised. This is intentional — search correctness depends on how Mongo evaluates the scope filter and the regex pattern.

## Test Helpers

Two group factory helpers set up the test topology:

```python
async def _mk_channel(ws, name, members) -> Group:
    # type="channel" — visible to any workspace member
async def _mk_private(ws, name, members) -> Group:
    # type="private" — visible only to members
```

`_mk_msg` inserts a `Message` document linked to a group. Together these helpers let each test compose an exact DB state before calling `MessageService.search_workspace`.

## Coverage

### Public channel visibility
`test_search_workspace_returns_public_channel_hits` verifies that a workspace member who is not explicitly listed as a group member can still find messages in a `channel`-type group. Channels are public by design; filtering them out by member list would be a correctness bug.

### Private room leakage prevention
`test_search_workspace_skips_private_non_members` is the privacy invariant test. It creates a private room with members `[u1, u2]` and searches as `u3`. The test asserts zero results, confirming that the search query includes a membership check for private groups. Without this guard, a malicious or misconfigured query could expose private message content across groups.

### Workspace scope isolation
`test_search_workspace_respects_workspace_scope` creates identical messages in workspace A and workspace B, then searches only workspace A. The result must contain only workspace-A hits. This prevents a multi-tenant data bleed where one organization's messages appear in another's search results.

### Regex metacharacter safety
`test_search_workspace_escapes_regex_metachars` passes a query containing regex special characters (e.g. `[`, `.`, `*`) and asserts that the results treat the input as a literal string, not a regex pattern. Without escaping, a user could craft a query that matches far more than intended or cause a regex parse error in MongoDB, both of which are security and stability concerns.

### Result cap
`test_search_workspace_caps_limit` verifies that the `limit` query parameter is clamped to a maximum of 100. A caller requesting 10,000 results must only receive up to 100. This prevents a fat-query denial-of-service where a single search overwhelms the DB with an unbounded result scan.

### Empty query short-circuit
`test_search_workspace_empty_query_returns_empty` asserts that an empty string query returns immediately with no results rather than executing a match-all regex against the messages collection. This is both a correctness guard (empty search is meaningless) and a performance guard (a `.*` regex on a large collection is expensive).

## Why Real Mongo Queries Matter Here

The scope filter (channel vs. private + member check) and the regex escape are both implemented at the query level. Mocking the DB layer would hide regressions in the query construction itself. The `beanie_memory_db` fixture provides a real query engine at the cost of slightly slower tests — an intentional tradeoff for this module.

## Known Gaps

No TODOs or FIXMEs are present. The test file does not cover pagination (cursor-based) behavior; a future sub-PR adding cursor support should add corresponding tests here.
