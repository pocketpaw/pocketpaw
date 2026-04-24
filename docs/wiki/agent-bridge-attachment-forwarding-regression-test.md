---
{
  "title": "Agent Bridge Attachment Forwarding Regression Test",
  "summary": "This regression test guards against the silent attachment-drop bug in `_run_agent_response`, where file attachments sent by users in channel messages were never forwarded into the agent's prompt. The test confirms that the channel code path formats attachment metadata — filename, MIME type, and size — into the prompt in the same shape used by the DM path.",
  "concepts": [
    "agent_bridge",
    "_run_agent_response",
    "attachment forwarding",
    "pool.run",
    "silent bug",
    "DM path",
    "channel path",
    "prompt construction",
    "async iterator",
    "Beanie mock",
    "regression test",
    "file attachments"
  ],
  "categories": [
    "agent bridge",
    "testing",
    "attachments",
    "regression",
    "test"
  ],
  "source_docs": [
    "4e961d2126deb45f"
  ],
  "backlinks": null,
  "word_count": 450,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `tests/cloud/shared/test_agent_bridge_attachments.py` module was created on 2026-04-19 to lock in a bug fix for a silent data loss condition. Before the fix, when a user sent a file attachment in a group channel, the `on_message_for_agents` handler and `_run_agent_response` function extracted only the text content. The `data["attachments"]` field was silently ignored, so `pool.run` received the user's message with no knowledge of the attached files.

Agents in group channels would therefore respond to "what's in this file?" with no context at all, producing confused or hallucinated answers. Because the failure was silent — no error, no log, just missing context — it was difficult to detect in integration testing.

## The Expected Format

The DM (direct message) path already formatted attachment metadata into the user prompt as:

```
Attached files:
- diagram.png (image/png, 47 KB) at /api/v1/uploads/abc123
```

The channel path must produce the same shape so agents reason about attachments consistently regardless of whether the conversation is a DM or a group channel. This test enforces that contract.

## Test Architecture

### `_AsyncIter`

A minimal async iterator that yields a fixed list of events and then raises `StopAsyncIteration`. This is used to replace `pool.run`, which is normally an async generator. The test yields only a `done` event with empty content, which triggers the bridge's `if not full_text.strip(): return` short-circuit before any Beanie persistence calls — keeping the test at unit scope without requiring a real database.

### Mocking Strategy

The test patches:
- `ee.cloud.shared.agent_bridge.emit` — prevents actual bus broadcasts
- `Message.find`, `Message.insert` — prevents DB queries without Beanie initialization
- `pocketpaw.agents.pool.get_agent_pool` — returns the controlled `pool` mock
- `KnowledgeService.search_context` — returns empty string to skip knowledge retrieval
- Beanie class-level query attributes (`group`, `deleted`, `createdAt`) — stamped as `MagicMock` because Beanie normally injects these at init time

```python
assert "diagram.png" in augmented, f"filename missing from prompt: {augmented!r}"
assert "image/png" in augmented, f"mime missing from prompt: {augmented!r}"
assert "Attached" in augmented or "attached" in augmented
```

The assertions check for presence of key tokens rather than exact format, allowing the human-readable size helper (`_format_bytes`) to change its output without breaking the test.

## Why This Pattern

Capturing `pool.run`'s `user_message` argument in a `captured` dict is a common PocketPaw pattern for verifying what the agent actually received. The test does not assert on the agent's response — only on what was passed in — because the contract being tested is prompt construction, not agent reasoning.

## Known Gaps

No TODO or FIXME markers. The test covers a single attachment. Multi-attachment scenarios and edge cases (zero-byte files, missing MIME types) are not covered. The `fake_run` short-circuits before persistence, so the full write path for agent messages with attachments is not exercised here.