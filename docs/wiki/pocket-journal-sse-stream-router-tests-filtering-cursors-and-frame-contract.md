---
{
  "title": "Pocket Journal SSE Stream Router Tests: Filtering, Cursors, and Frame Contract",
  "summary": "Tests the Server-Sent Events (SSE) endpoint that streams journal events for a specific pocket to the RippleGraphWidget frontend. Covers the event-matching predicate, backlog replay with `since_seq` cursor support for reconnects, the `connected` handshake frame, idle-poll timeout, and strict isolation so events from other pockets never appear in a pocket's stream.",
  "concepts": [
    "SSE",
    "Server-Sent Events",
    "pocket journal",
    "event filtering",
    "since_seq cursor",
    "RippleGraphWidget",
    "dependency_overrides",
    "connected handshake",
    "idle-poll timeout",
    "soul_protocol journal"
  ],
  "categories": [
    "testing",
    "streaming",
    "enterprise features",
    "real-time",
    "test"
  ],
  "source_docs": [
    "3981902eafddf5c0"
  ],
  "backlinks": null,
  "word_count": 504,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/ee/test_pocket_journal_stream.py` was created in `feat/cluster-b-ripple-journal-stream` as part of Cluster B / Wave 3 §11 for the RippleGraphWidget. The route it covers streams a filtered view of the shared org journal over SSE to a frontend widget that visualises pocket activity in real time.

## Why SSE and Why This Filtering

The org journal stores events from every pocket, retrieval call, widget interaction, and system action in one SQLite database. Sending the raw stream to a widget would be both a data-leak risk and a performance problem. The stream router filters events to those where `payload.pocket_id == <path_pocket>` or the event's scope list contains an entry starting with `pocket:<id>`. This dual-path matching exists because different parts of the system write pocket affinity in different places — payload for most event types, scope for legacy entries.

## Test Class Breakdown

### TestEntryMatchesPocket
Direct unit tests of the `_entry_matches_pocket(entry, pocket_id)` predicate that the router delegates to:
- `test_matches_by_payload_pocket_id` — canonical path used by Widget + Retrieval stores.
- `test_matches_by_scope_pocket_prefix` — fallback for entries that encode affinity in scope.
- `test_does_not_match_dataref_payload` — DataRef entries that share a payload structure should not false-positive.

Testing the predicate in isolation before testing the full SSE route is deliberately layered — a predicate bug would otherwise manifest as an opaque "wrong number of SSE frames" failure.

### TestStreamPocketJournal
These tests mount the real router on a minimal FastAPI app with `dependency_overrides` disabling auth and license checks:

```python
a.dependency_overrides[require_pocket_edit] = lambda pocket_id: None
a.dependency_overrides[require_license] = lambda: None
```

Key scenarios:
- **Connected handshake**: The first SSE frame must be `event: connected` with the current `last_seq` so the widget can store a resume cursor before processing backlog. Without this frame, a widget that reconnects mid-session would not know where to resume.
- **Backlog filtering**: Only events matching the target pocket appear in the stream; events from other pockets are silently dropped.
- **since_seq cursor**: When the client reconnects with `?since_seq=N`, the router replays only events with `seq > N`. This prevents the widget from re-rendering the entire history on every network blip.
- **Frame shape**: Each `event: journal` frame carries a JSON body with the fields the widget reader consumes. The test asserts specific keys rather than just checking the frame count.
- **Empty pocket**: A pocket with no events still emits the `connected` handshake — the widget must not hang waiting for it.
- **Idle close**: After `max_idle_polls` without new events the stream closes cleanly, freeing the server-side connection.

## SSE Frame Parser

```python
def _read_sse_frames(response_text: str) -> list[dict]:
    frames = []
    for block in response_text.split("\n\n"):
        if not block.strip() or block.strip().startswith(":"):
            continue
        ...
    return frames
```

Keepalive comment lines (`: keepalive`) are intentionally discarded. Their presence in production prevents proxies from closing idle connections, but they are noise for frame-count assertions.

## Known Gaps

Auth and license checks are stubbed out — access-control matrix coverage is delegated to the Cluster B security-auditor. There is no test for the case where the journal backend raises mid-stream (e.g., SQLite file deleted during streaming).