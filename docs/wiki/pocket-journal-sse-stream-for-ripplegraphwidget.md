---
{
  "title": "Pocket Journal SSE Stream for RippleGraphWidget",
  "summary": "The journal stream router serves a live Server-Sent Events feed of soul-protocol journal entries filtered to a single pocket, enabling the frontend RippleGraphWidget to render agent decisions, retrievals, and tool traces as a live causation graph. It supports resumable cursors, cold-start backlog replay, and keepalive heartbeats to survive proxy timeouts.",
  "concepts": [
    "SSE",
    "Server-Sent Events",
    "journal stream",
    "RippleGraphWidget",
    "resumable cursor",
    "since_seq",
    "soul-protocol",
    "EventEntry",
    "SQLite WAL",
    "causation graph",
    "keepalive",
    "pocket filtering"
  ],
  "categories": [
    "pockets",
    "realtime",
    "streaming",
    "agent-integration",
    "enterprise-cloud"
  ],
  "source_docs": [
    "99b5bba5b8cdd8fb"
  ],
  "backlinks": null,
  "word_count": 618,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `journal_stream_router.py` exposes `GET /pockets/{pocket_id}/journal/stream`, a Server-Sent Events (SSE) endpoint that streams soul-protocol `EventEntry` records scoped to one pocket. The frontend RippleGraphWidget subscribes to this stream to render a live causal graph of agent activity — decisions, retrievals, tool calls — as they happen.

## Why SSE Over WebSocket?

The stream is read-only: the client receives events but never sends any. SSE is the correct transport for unidirectional server-push streams. It uses plain HTTP/1.1 chunked transfer, survives HTTP/2 multiplexing, and browsers handle reconnection automatically via the `EventSource` API without application code.

## Dual-Source Pocket Filtering

Journal events can tag a pocket in two ways, and the router accepts both:

1. **`payload.pocket_id == pocket_id`** — the convention used by `ee/widget/store.py` and `ee/retrieval/store.py`.
2. **`scope` entry `"pocket:<id>"`** — a proposed namespace for callers that prefer scope tagging.

`_entry_matches_pocket` checks the scope list first (O(n) over scope entries, typically small) then inspects the payload dict. A `DataRef` payload (zero-copy external data reference) short-circuits to scope-only matching, since `DataRef` carries no inline payload fields.

Accepting both patterns means the stream works for existing event producers without a coordinated migration.

## Resumable Cursor with `since_seq`

Each SSE event carries a `seq` integer from the journal's SQLite backend. The client sends `since_seq` as a query parameter on connect (or reconnect). The stream replays all events with `seq > since_seq`, then switches to polling.

`since_seq = -1` is the cold-start sentinel. Because journal sequences start at 0, a naive `since_seq + 1 = 0` start would include seq 0; the sentinel is used instead of `0` to make the intent explicit and to distinguish "first ever connect" from "reconnect after seq 0".

The cursor is advanced even for filtered-out events (`last_seq` tracks the last observed sequence regardless of pocket match), so the next poll does not re-walk the same prefix.

## SQLite Backend Access via `journal._backend`

The function `_drain_since` accesses `journal._backend` and `backend._conn` directly, bypassing the public `Journal.query` API. The code comment explains why: the public API flattens `seq` away and `soul-protocol 0.3.1` does not populate `EventEntry.seq` on round-tripped entries. Without direct backend access, there is no way to maintain a resumable sequence cursor. This is marked as a known workaround pending an upstream API fix.

## Polling Cadence and Heartbeat

- **`POLL_INTERVAL_SEC = 0.5`** — 500ms polling keeps perceived latency under the RippleGraphWidget's `<2s` node-visibility requirement while adding at most 2 QPS per connection against the SQLite WAL reader.
- **`HEARTBEAT_SEC = 30.0`** — SSE comment frames (`": keepalive"`) every 30 seconds prevent proxy and load balancer idle-timeout teardowns. Matches the convention used by the chat events SSE router.
- **`X-Accel-Buffering: no`** header disables nginx's response buffering so SSE frames flush to the client immediately rather than accumulating in a buffer.

## `max_idle_polls` Debug Parameter

A `max_idle_polls` query parameter exists exclusively for testing: it closes the stream after N idle polls, producing a terminal `event: closed` frame. Production `EventSource` clients never pass this parameter. This avoids the need for test-specific mock infrastructure to terminate an otherwise infinite stream.

## Three Event Types

1. **`event: connected`** — handshake with `last_seq` and `pocket_id` so the client can pin its cursor.
2. **`event: journal`** — one per matched `EventEntry`.
3. **`": keepalive"`** — SSE comment; not an event, just a heartbeat to prevent connection teardown.

## Known Gaps

- `_drain_since` accesses `journal._backend` and `backend._conn` directly — a private API that could break on soul-protocol upgrades.
- `INITIAL_BACKLOG_LIMIT = 200` caps cold-start replay, so a pocket with more than 200 historical events will not fully replay them on first connect.
- No workspace authorization beyond `require_pocket_edit` — any user with pocket-edit access can stream the full journal, including entries from other agents in the same pocket.