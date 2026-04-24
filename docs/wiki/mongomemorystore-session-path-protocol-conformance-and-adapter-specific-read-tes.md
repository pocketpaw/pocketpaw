---
{
  "title": "MongoMemoryStore SESSION Path: Protocol Conformance and Adapter-Specific Read Tests",
  "summary": "These tests cover the full SESSION memory path in MongoMemoryStore — CRUD, chronological message retrieval, session key isolation, combined session-with-messages reads with limit behavior, and cross-collection isolation between pocket and group messages.",
  "concepts": [
    "MongoMemoryStore",
    "SESSION memory",
    "session_key",
    "get_session",
    "get_session_with_messages",
    "role validation",
    "chronological ordering",
    "message isolation",
    "clear_session",
    "pocket messages",
    "MemoryStoreProtocol"
  ],
  "categories": [
    "testing",
    "memory store",
    "session management",
    "MongoDB",
    "test"
  ],
  "source_docs": [
    "664a9356f5b9fa73"
  ],
  "backlinks": null,
  "word_count": 308,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The SESSION memory path stores conversational turns — user and assistant messages — for pocket AI sessions. Session messages have a `session_key` and `role`, and are retrieved in chronological order for LLM context injection. This file pins the complete contract for that path.

## Role Validation at Save Time

```python
async def test_save_rejects_invalid_role_via_validator(self, store):
    bad = MemoryEntry(id="", type=MemoryType.SESSION, content="x",
                      role="attacker", session_key=key)
    with pytest.raises(ValueError):
        await store.save(bad)
```

The Message model's `model_validator` only accepts `user`, `assistant`, and `system`. An invalid role could corrupt LLM context by injecting a fake system prompt or an unrecognized role marker.

## Chronological Ordering

```python
async def test_get_session_returns_messages_ascending(self, store):
    for i in range(3):
        await store.save(_entry(key, "user" if i % 2 == 0 else "assistant", f"m{i}"))
        await asyncio.sleep(0.01)
    got = await store.get_session(key)
    assert [e.content for e in got] == ["m0", "m1", "m2"]
```

LLM context injection requires messages oldest-first. The `asyncio.sleep(0.01)` ensures distinct `createdAt` timestamps in an environment where inserts could otherwise share the same millisecond.

## Combined Read: get_session_with_messages

```python
async def test_get_session_with_messages_limit_returns_recent_ascending(self, store):
    _, messages = await store.get_session_with_messages(key, limit=3)
    assert [m.content for m in messages] == ["m2", "m3", "m4"]
```

When a limit is applied, the store returns the **most recent N messages in ascending order**. For long-running conversations, you want the last N turns as context, in correct chronological order for the LLM. A naive descending slice would give wrong ordering.

## Cross-Collection Isolation

```python
async def test_returns_pocket_messages_only(self, store):
    await store.save(_entry("sess1", "user", "pocket-row"))
    await Message(context_type="group", group="g1", ...).insert()
    got = await store.get_by_type(MemoryType.SESSION, limit=100)
    assert "group-row" not in [e.content for e in got]
```

Group and pocket messages share the `Message` Beanie document but are filtered by `context_type`. `get_by_type(SESSION)` must not surface group rows.

## Edge Cases

- `get("not-an-object-id")` returns `None`.
- `delete("bogus")` returns `False` — idempotent for malformed IDs.
- `get_session("no-such-session")` returns `[]`.
- `clear_session("never-existed")` returns `0`.

## Known Gaps

None identified.