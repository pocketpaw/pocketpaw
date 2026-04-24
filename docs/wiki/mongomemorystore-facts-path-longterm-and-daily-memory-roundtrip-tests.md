---
{
  "title": "MongoMemoryStore Facts Path: LONG_TERM and DAILY Memory Roundtrip Tests",
  "summary": "These tests exercise the complete facts path of MongoMemoryStore, covering CRUD, type isolation between LONG_TERM and DAILY entries, cross-type search, regex safety, and protocol conformance. They also verify that user_id is stored as a dedicated column and never duplicated in the metadata dictionary.",
  "concepts": [
    "MongoMemoryStore",
    "MemoryEntry",
    "LONG_TERM",
    "DAILY",
    "memory_facts",
    "get_by_type",
    "search",
    "regex escaping",
    "user_id column",
    "MemoryStoreProtocol",
    "type isolation",
    "CRUD"
  ],
  "categories": [
    "testing",
    "memory store",
    "MongoDB",
    "data persistence",
    "test"
  ],
  "source_docs": [
    "50d6e906e5e51ba1"
  ],
  "backlinks": null,
  "word_count": 415,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`MongoMemoryStore` manages two categories of persistent AI memory: `LONG_TERM` facts (user preferences, learned knowledge) and `DAILY` facts (ephemeral journal entries). These live in a dedicated `memory_facts` collection, separate from the `memory_messages` collection used for SESSION turn history. This test file pins the complete contract for that facts path.

## Why Two Fact Types Exist

`LONG_TERM` facts survive indefinitely and are typically associated with a specific user via `user_id`. `DAILY` facts are time-bounded observations that don't require user attribution. Keeping them in the same collection with a `type` discriminator allows both type-scoped queries and cross-type search in a single call.

## CRUD Roundtrip

```python
async def test_save_long_term_returns_objectid_hex(self, store):
    entry_id = await store.save(_long_term("user prefers dark mode"))
    assert isinstance(entry_id, str) and len(entry_id) == 24
```

The return value is always a 24-character hex MongoDB ObjectId string. Callers use it for `get()` and `delete()` lookups. Any non-hex string is treated as invalid rather than raising.

## Type Isolation

```python
async def test_daily_does_not_leak_into_long_term(self, store):
    await store.save(_long_term("a long-term fact"))
    await store.save(_daily("a daily note"))
    lt = await store.get_by_type(MemoryType.LONG_TERM)
    dl = await store.get_by_type(MemoryType.DAILY)
    assert "a daily note" not in [e.content for e in lt]
    assert "a long-term fact" not in [e.content for e in dl]
```

Both types share a collection. Without a strict type filter, DAILY facts could appear in LONG_TERM results, polluting user preference lookups with transient observations.

## Search Safety: Regex Escaping

```python
async def test_regex_metacharacters_are_escaped(self, store):
    await store.save(_long_term("user paid $100 for the plan"))
    got = await store.search(query="$100", memory_type=MemoryType.LONG_TERM, limit=5)
    assert len(got) == 1
```

MongoDB regex search without escaping would interpret `$100` as an anchor pattern, returning zero results or the wrong set. The store must call `re.escape()` before building the query.

The untyped search (no `memory_type`) spans LONG_TERM and DAILY facts but excludes SESSION messages, which live in a different collection.

## user_id Column Separation

```python
async def test_user_id_metadata_not_duplicated(self, store):
    entry_id = await store.save(_long_term("owned", user_id="u7"))
    raw = await MemoryFactDoc.get(PydanticObjectId(entry_id))
    assert raw.user_id == "u7"
    assert "user_id" not in raw.metadata
```

`user_id` is a first-class indexed column on `MemoryFactDoc`. Storing it again inside `metadata` would waste space, confuse `get_by_type(user_id=...)` filter logic, and create a dual-write consistency hazard.

## Protocol Conformance

```python
def test_satisfies_memory_store_protocol(self):
    store: MemoryStoreProtocol = MongoMemoryStore()
    for method in ("save","get","delete","search","get_by_type","get_session","clear_session"):
        assert callable(getattr(store, method))
```

This structural check ensures `MongoMemoryStore` can be swapped in anywhere `MemoryStoreProtocol` is expected.

## Edge Cases

- `get("not-an-id")` returns `None` — callers don't need to pre-validate IDs.
- `delete("not-an-id")` returns `False` — idempotent for missing or malformed IDs.

## Known Gaps

None identified.