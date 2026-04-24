---
{
  "title": "Agent Seed Idempotency and DM Chat Persistence with Attachments Tests",
  "summary": "This module tests the `seed_default_agent` function for idempotency and per-workspace isolation, validates that `MongoMemoryStore.save` persists attachments to the `Message` document and updates the session's `messageCount` and `lastActivity`, and verifies that `SessionService.get_history` returns attachments and `SessionService.list_by_agent` applies soft-delete filtering.",
  "concepts": [
    "seed_default_agent",
    "MongoMemoryStore",
    "SessionService",
    "idempotency",
    "soft delete",
    "attachments",
    "DM persistence",
    "Beanie",
    "mongomock",
    "list_by_agent",
    "get_history",
    "backfill"
  ],
  "categories": [
    "agents",
    "sessions",
    "testing",
    "database",
    "attachments",
    "test"
  ],
  "source_docs": [
    "c925859ec10f1d6c"
  ],
  "backlinks": null,
  "word_count": 463,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `tests/cloud/test_agent_seed_and_dm_persistence.py` module covers two critical initialization and persistence concerns in PocketPaw's cloud backend: agent seeding and DM message storage with file attachments.

## Fixture: `beanie_db`

All tests use a shared `beanie_db` fixture that spins up an isolated `mongomock_motor` database, registers all EE documents with Beanie, and yields the database handle. A unique database name per test prevents state leakage. The fixture patches `list_collection_names` to avoid a `mongomock` compatibility issue with Beanie's collection introspection.

## TestSeedDefaultAgent

### Idempotency

`seed_default_agent` creates the PocketPaw companion agent for a workspace on first call. The idempotency test calls it twice for the same workspace and asserts that the second call returns `created=False` and that only one agent exists in the database. Without this guard, concurrent workspace setup requests (e.g., during onboarding) could create duplicate companion agents, breaking the slug-uniqueness invariant.

### Per-Workspace Isolation

The `test_per_workspace_agents` test seeds agents for `ws-1` and `ws-2` independently, confirming each workspace gets its own companion agent and neither interferes with the other. This is important because the agent slug `"pocketpaw"` must be unique per workspace, not globally.

## TestEnsureDefaultAgentBackfill

The backfill function creates companion agents for all existing workspaces that lack one — typically run as a migration after the feature is introduced. The tests verify that the backfill creates agents for workspaces without them, and that running the backfill twice does not create duplicates.

## TestMongoMemoryStoreAttachments

`MongoMemoryStore.save` is the canonical write path for user messages in DM conversations. Attachments arrive in `entry.metadata["attachments"]`. The tests confirm:

- When `metadata["attachments"]` contains attachment dicts, the `Message` document's `attachments` field is populated.
- When the `attachments` key is absent from metadata, the field defaults to an empty list — not `None`, which would break JSON serialization.
- The session's `messageCount` is incremented and `lastActivity` is updated on every save.

```python
async def test_no_attachments_key_yields_empty_list(self, beanie_db) -> None:
    # Ensures the Message document always has a list, never None
    ...
    assert message.attachments == []
```

## TestHistoryReturnsAttachments

`SessionService.get_history` must include attachments in the per-message history entries. This test creates a message with attachments, then retrieves history and asserts the attachment data round-trips correctly. This is critical for the chat UI to re-render previously sent files.

## TestListByAgent

`SessionService.list_by_agent` must:
1. Filter sessions to those belonging to a specific agent.
2. Respect soft-delete (sessions with `deleted_at` set must not appear in the results).

The soft-delete test creates two sessions — one active, one soft-deleted — and asserts only the active one is returned. Without this filter, deleted sessions would pollute the agent's session list.

## Known Gaps

No TODO or FIXME markers. The `mongomock_motor` fixture does not exercise MongoDB indexing or query performance. The backfill tests do not cover workspaces with multiple users. Attachment handling for edge cases (malformed metadata dicts, oversized files) is not tested.