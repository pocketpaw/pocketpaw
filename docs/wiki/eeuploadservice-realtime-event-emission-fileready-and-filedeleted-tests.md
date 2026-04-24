---
{
  "title": "EEUploadService Realtime Event Emission: FileReady and FileDeleted Tests",
  "summary": "This suite verifies that `EEUploadService` fires `FileReady` events over the realtime bus when files are uploaded to a chat-scoped context, and `FileDeleted` events when those files are removed. It also verifies that neither event fires when the upload or file lacks a `chat_id`, preventing noise from workspace-level background operations.",
  "concepts": [
    "FileReady",
    "FileDeleted",
    "realtime events",
    "emit",
    "chat_id scoping",
    "BulkUploadResult",
    "EEUploadService",
    "monkeypatch",
    "WebSocket notifications",
    "event payload"
  ],
  "categories": [
    "testing",
    "realtime events",
    "file uploads",
    "enterprise edition",
    "test"
  ],
  "source_docs": [
    "9c76a6a40d82cd8c"
  ],
  "backlinks": null,
  "word_count": 508,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's enterprise upload service does two things after a successful storage write: it persists a metadata record, and it notifies the realtime bus so connected clients can react without polling. This test file isolates the emission logic from the storage layer entirely, using `__new__`-constructed service instances with hand-injected stubs.

## Why Emit at All?

Realtime file availability notifications are required for chat UI update flows. When an agent or user uploads a file into a chat group, other connected participants need to see the file appear immediately. Without a `FileReady` event the client would only discover the file on the next page load or explicit refresh.

## The `_capture_emits` Helper

```python
def _capture_emits():
    recorded: list = []
    async def fake_emit(ev):
        recorded.append(ev)
    return recorded, fake_emit
```

This helper produces a pair — a mutable list and an async callable that appends to it. `monkeypatch.setattr("ee.cloud.uploads.service.emit", fake_emit)` replaces the module-level bus reference so no real pub/sub infrastructure is needed. The approach avoids `MagicMock` for the emit target because the real emit is async; using `AsyncMock` would hide accidental double-invocation bugs that the explicit list makes visible.

## Chat-Scoped vs. Non-Chat Files

The key design decision the tests encode is that emission is conditional on `chat_id`:

- Files uploaded with a non-`None` `chat_id` emit `FileReady` per file.
- Files uploaded with `chat_id=None` (workspace-level uploads, background ingestion, etc.) must not emit `FileReady`.

```python
async def test_upload_many_no_emit_when_chat_id_is_none(monkeypatch):
    inner_result = BulkUploadResult(uploaded=[_rec("f1", chat_id=None)], failed=[])
    ...
    await svc.upload_many([], owner_id="u1", chat_id=None, workspace="w1")
    assert not any(isinstance(e, FileReady) for e in recorded)
```

This guard prevents workspace-level file operations (e.g., admin bulk imports) from flooding every chat participant's UI with spurious file notifications.

## FileReady Payload Contract

`test_upload_many_emits_file_ready_when_chat_scoped` asserts the exact shape of every emitted `FileReady` event:

```python
for e in file_events:
    assert e.data["group_id"] == "g1"
    assert e.data["filename"] == "hello.txt"
    assert e.data["mime"] == "text/plain"
    assert e.data["size"] == 11
    assert "url" in e.data
```

Each field serves a purpose: `group_id` routes the event to the right WebSocket audience, `filename`/`mime`/`size` let the UI render a preview card without a follow-up fetch, and `url` provides a direct download link so the client never has to reconstruct it from `file_id` alone.

## FileDeleted Payload Contract

For deletion, the payload is intentionally minimal:

```python
assert file_events[0].data == {"group_id": "g1", "file_id": "f1"}
```

No filename, no size — because those are only needed to render a new card. Deletion only needs enough for the client to identify which card to remove from the DOM.

## Structural Isolation via `__new__`

The tests construct `EEUploadService` via `EEUploadService.__new__(EEUploadService)` and manually assign `_oss`, `_meta`, and `_adapter` attributes. This bypasses `__init__` entirely, ensuring the tests only exercise the emission logic and not the dependency-injection wiring. It also means the tests continue to pass even when the constructor signature changes.

## Known Gaps

There is no test covering the case where `emit` itself raises. If the realtime bus is unavailable, the current code may silently swallow or propagate the error depending on how `emit` is implemented. A future hardening pass should add a test asserting graceful degradation when emission fails.