---
{
  "title": "Smoke Test: MongoDB Memory Backend End-to-End",
  "summary": "This script exercises the full MongoDB memory path from `init_cloud_db` through `MemoryManager.add_to_session`, `get_session_history`, raw Mongo inspection, and long-term memory storage in `memory_facts`. It serves as the canonical integration check for the enterprise memory backend.",
  "concepts": [
    "MongoDB memory backend",
    "MemoryManager",
    "add_to_session",
    "get_session_history",
    "MongoMemoryStore",
    "MemoryFactDoc",
    "context_type",
    "long-term memory",
    "init_cloud_db",
    "memory_facts",
    "smoke test"
  ],
  "categories": [
    "testing",
    "memory",
    "MongoDB",
    "backend initialization"
  ],
  "source_docs": [
    "7981b5fd586a7839"
  ],
  "backlinks": null,
  "word_count": 425,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Purpose

This smoke test validates the entire MongoDB memory stack the way a running enterprise server would use it. It is the broadest memory-layer test and acts as a regression harness for any change touching `init_cloud_db`, `MemoryManager`, `MongoMemoryStore`, or the `MemoryFactDoc` model.

## Initialization: `init_cloud_db` Flips the Backend

The first step calls `init_cloud_db(mongo_uri)` and then checks:

```python
assert os.environ["POCKETPAW_MEMORY_BACKEND"] == "mongodb"
```

This confirms the function both connects to MongoDB and sets the environment variable that downstream code reads to select the correct store. The env-var check is important: other components (e.g., worker processes spawned later) read `POCKETPAW_MEMORY_BACKEND` at startup, not from the in-process singleton.

## Session Memory Round-Trip

Steps 3 and 4 test the two core `MemoryManager` methods:

- `add_to_session(session_key, role, content)` → returns a 24-character hex MongoDB ObjectId
- `get_session_history(session_key)` → returns a list of `{"role": ..., "content": ...}` dicts in insertion order

The history format is designed to be passed directly to an LLM as the `messages` array. The test asserts exact content and ordering:

```python
assert history == [
    {"role": "user", "content": "Hello, PocketPaw!"},
    {"role": "assistant", "content": "Hi — how can I help?"},
]
```

## Raw Mongo Assertions

Step 5 bypasses `MemoryManager` entirely and queries the `messages` collection directly via the Beanie ODM:

```python
rows = await Message.find({"session_key": session_key}).to_list()
assert r.context_type == "pocket"
assert not r.group
```

The `context_type="pocket"` constraint distinguishes session (AI chat) messages from group/DM messages. The `not r.group` assertion ensures pocket messages are never accidentally linked to a chat group, which would cause them to appear in group chat histories.

## Long-Term Memory

Step 6 writes a fact to the `memory_facts` collection:

```python
long_term_id = await manager.remember("Smoke test user prefers concise replies", tags=["preferences"])
fact = await MemoryFactDoc.get(long_term_id)
assert fact.type == "long_term"
assert "preferences" in fact.tags
```

Long-term memories are persistent user facts (preferences, biographical details) stored separately from session messages. They feed the retrieval-augmented memory context injected into LLM prompts. The `tags` field enables faceted retrieval (e.g., all `preferences` facts for a user).

## Database Lifecycle

The script generates a throwaway database name, runs all steps, then tears down completely:

```python
await close_cloud_db()
client = AsyncIOMotorClient("mongodb://localhost:27017")
await client.drop_database(db_name)
```

Calling `close_cloud_db()` before dropping ensures all Beanie connections are released, preventing resource leaks on repeated runs.

## Usage

```
uv run python scripts/smoke_mongo_memory.py
```

Override the MongoDB URI with `POCKETPAW_SMOKE_MONGO_URI` if your instance is not on `localhost:27017`.

## Known Gaps

The test does not cover concurrent writes, session expiry, or memory retrieval by tag. The `remember()` call is tested but `recall()` (tag-based search) is not exercised here.