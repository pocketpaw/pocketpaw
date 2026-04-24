---
{
  "title": "MongoMemoryStore Session Index Shape and Filtering Contract Tests",
  "summary": "These tests verify `_load_session_index_async` in MongoMemoryStore, which powers the `GET /sessions/runtime` endpoint. They pin the exact dict shape, filtering of group and soft-deleted sessions, channel inference from session key prefix, and empty title coercion.",
  "concepts": [
    "_load_session_index_async",
    "session index",
    "GET /sessions/runtime",
    "channel inference",
    "soft delete",
    "group session exclusion",
    "title coercion",
    "MongoMemoryStore",
    "sidebar",
    "pocket session",
    "backend-agnostic"
  ],
  "categories": [
    "testing",
    "API",
    "session management",
    "memory store",
    "test"
  ],
  "source_docs": [
    "6c2746fb457c5b0b"
  ],
  "backlinks": null,
  "word_count": 302,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`_load_session_index_async` is the MongoDB counterpart to the file store's `_load_session_index`. Both return a dict keyed by `session_id` with four fields: `title`, `channel`, `last_activity`, and `message_count`. The contract must be identical so the `GET /sessions/runtime` router works without modification regardless of which backend is active.

## Why This Method Exists

The sessions index is the data source for the sidebar in PocketPaw's UI. When a user opens the dashboard, the frontend calls `/sessions/runtime` to render the list of recent conversations. The index must be fast (no per-session queries) and must surface only pocket sessions, not group channels or deleted sessions.

## Shape Contract

```python
async def test_returns_entry_with_expected_shape(self, store):
    entry = index["websocket_abc123"]
    assert entry == {
        "title": "Hello world",
        "channel": "websocket",
        "last_activity": "2026-04-10T12:00:00+00:00",
        "message_count": 3,
    }
```

`last_activity` is serialized as ISO 8601 with timezone offset, matching what the file store returns and what frontend JavaScript `Date.parse()` accepts.

## Group Session and Soft Delete Filtering

Group sessions represent channel conversations, not personal AI sessions. The sidebar only shows pocket sessions, so the query filters `context_type == "pocket"`.

Soft deletes are handled with a `deleted_at` timestamp. The index query filters `deleted_at == None` to avoid surfacing deleted conversations.

## Channel Inference from Key Prefix

Session keys follow the convention `<channel>_<id>`. The store splits on the first underscore to derive the channel:

```python
async def test_channel_fallback_for_keys_without_underscore(self, store):
    await _make_session(session_id="noprefix")
    assert index["noprefix"]["channel"] == "unknown"
```

If no underscore is present, the channel falls back to `"unknown"`. The frontend must handle this gracefully.

## Empty Title Coercion

```python
async def test_empty_title_coerced_to_default(self, store):
    await _make_session(session_id="websocket_x", title="")
    assert index["websocket_x"]["title"] == "New Chat"
```

An API consumer could explicitly set title to an empty string. The index method coerces empty titles to the default `"New Chat"` so the sidebar never shows a blank entry.

## Known Gaps

None identified.