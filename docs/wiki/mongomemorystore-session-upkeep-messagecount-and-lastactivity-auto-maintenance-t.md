---
{
  "title": "MongoMemoryStore Session Upkeep: messageCount and lastActivity Auto-Maintenance Tests",
  "summary": "These tests verify that MongoMemoryStore automatically updates Session metadata (messageCount, lastActivity) on every save and auto-creates missing Session rows when a message arrives before the session is explicitly created. They pin behaviors introduced after a historical refactor that removed a broken parallel bus subscriber.",
  "concepts": [
    "MongoMemoryStore",
    "session upkeep",
    "messageCount",
    "lastActivity",
    "auto-create session",
    "single write path",
    "chat_persistence",
    "bus subscriber refactor",
    "pocket session",
    "graceful degradation",
    "session key normalization"
  ],
  "categories": [
    "testing",
    "session management",
    "memory store",
    "data consistency",
    "test"
  ],
  "source_docs": [
    "08fa58ae608a461a"
  ],
  "backlinks": null,
  "word_count": 275,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Historical Context

Before the current implementation, session metadata upkeep lived in a separate `chat_persistence` bus subscriber. When that subscriber was deleted to make `MongoMemoryStore.save` the single write path, session documents stopped being updated and sessions appeared frozen in the sidebar. These tests pin the upkeep into the store's contract so any future refactor that drops it fails immediately.

## Upkeep on Existing Sessions

```python
async def test_save_touches_existing_session(self, store) -> None:
    await session.insert()
    assert session.messageCount == 0
    prior_activity = session.lastActivity

    await store.save(_entry("websocket:upk-1", "first"))
    await store.save(_entry("websocket:upk-1", "second"))

    reloaded = await Session.find_one(Session.sessionId == "websocket_upk-1")
    assert reloaded.messageCount == 2
    assert reloaded.lastActivity != prior_activity
```

Note the key format: the session document uses `websocket_upk-1` (underscore separator) while the memory entry uses `websocket:upk-1` (colon separator). The store normalizes between these conventions when linking message writes to their parent Session row.

## Auto-Creation of Missing Sessions

```python
async def test_save_auto_creates_pocket_session_when_missing(self, store) -> None:
    await store.save(_entry("websocket:new-1", "hello"))
    session = await Session.find_one(Session.sessionId == "websocket_new-1")
    assert session is not None
    assert session.context_type == "pocket"
    assert session.messageCount == 1
```

The `/chat/stream` endpoint skips `POST /sessions` on the first turn — it relies on the store to bootstrap the Session row. Without auto-creation, new conversations would exist in the messages collection but be invisible in the sidebar.

## Graceful Degradation Without Users

```python
async def test_save_without_any_user_still_persists_message(self, store) -> None:
    await store.save(_entry("websocket:lonely", "hi"))
    msg = await Message.find_one(Message.session_key == "websocket_lonely")
    assert msg is not None
    session = await Session.find_one(Session.sessionId == "websocket_lonely")
    assert session is None
```

On a fresh installation with no users, the store cannot auto-create a Session row (no workspace to assign). The message must still be persisted without raising.

## Known Gaps

None identified.