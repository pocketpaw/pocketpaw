---
{
  "title": "MongoMemoryStore Multi-Tenant Workspace Isolation Tests",
  "summary": "These tests verify that MongoMemoryStore stamps workspace_id onto both session messages and fact documents at write time, and that read operations are scoped to the requested workspace to prevent cross-tenant data leakage. Untagged rows with workspace_id=None must never appear in workspace-scoped queries.",
  "concepts": [
    "workspace isolation",
    "multi-tenancy",
    "workspace_id stamping",
    "MongoMemoryStore",
    "get_session_in_workspace",
    "list_facts_in_workspace",
    "untagged rows",
    "MemoryFactDoc",
    "defense-in-depth",
    "tenant isolation",
    "column promotion"
  ],
  "categories": [
    "testing",
    "security",
    "multi-tenancy",
    "memory store",
    "test"
  ],
  "source_docs": [
    "191436fcd53867c0"
  ],
  "backlinks": null,
  "word_count": 330,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw supports multiple workspaces on a single MongoDB cluster. Without rigorous workspace isolation, a query for workspace A could surface data from workspace B — a serious multi-tenant security failure. These tests pin the workspace stamping and filtering logic across both the SESSION path (messages) and the facts path.

## Workspace Stamping: Three Resolution Strategies

The store uses a priority chain to determine which workspace_id to stamp onto each write:

1. **Explicit metadata**: If `MemoryEntry.metadata` contains `workspace_id`, that value is used directly.
2. **Session doc lookup**: If the entry has a `session_key` and there is a matching Session document, the store reads `session.workspace`.
3. **Unresolvable**: If neither source is available, `workspace_id` is stored as `None`.

```python
async def test_session_save_resolves_workspace_from_session_doc(self, store):
    await Session(sessionId=key, context_type="pocket",
                  workspace="ws-from-session", owner="u1").insert()
    await store.save(_entry(key, "user", "implicit"))
    rows = await Message.find({"session_key": key}).to_list()
    assert rows[0].workspace_id == "ws-from-session"
```

The session doc lookup is the common production path — new messages inherit their session's workspace without the caller having to pass it explicitly.

## Why Untagged Rows Must Not Match

Unresolvable rows get `workspace_id=None`. The read-side filters use exact match (`workspace_id == "ws-X"`), so `None` never matches any workspace query. This is the safe default: fail closed rather than leaking data into the wrong tenant's view.

## Cross-Tenant Read Isolation

```python
async def test_get_session_in_workspace_filters_other_tenants(self, store):
    a_in_a = await store.get_session_in_workspace(key_a, "ws-A")
    a_in_b = await store.get_session_in_workspace(key_a, "ws-B")
    assert [m.content for m in a_in_a] == ["from A"]
    assert a_in_b == []
```

Even though `session_key` uniqueness prevents most collisions in practice, the explicit workspace filter is defense-in-depth against any future bug that allows key collisions across workspaces.

## Fact Column Separation

```python
async def test_fact_save_stamps_workspace_from_metadata(self, store):
    raw = await MemoryFactDoc.find({"content": "user prefers dark mode"}).to_list()
    assert raw[0].workspace_id == "ws-1"
    assert "workspace_id" not in raw[0].metadata
    assert "user_id" not in raw[0].metadata
```

Both `workspace_id` and `user_id` are promoted from metadata to first-class indexed columns on `MemoryFactDoc`. This keeps metadata clean and enables index scans for workspace and user filters.

## Known Gaps

None identified.