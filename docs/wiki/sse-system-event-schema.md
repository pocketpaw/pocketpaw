---
{
  "title": "SSE System Event Schema",
  "summary": "Defines the Pydantic model for system events delivered to clients over Server-Sent Events (SSE). This thin schema ensures every event has a typed identity and an optional payload, keeping the SSE stream contract explicit and validated.",
  "concepts": [
    "SSE",
    "Server-Sent Events",
    "SystemEventData",
    "Pydantic",
    "BaseModel",
    "event_type",
    "metadata",
    "push notifications",
    "agent runtime",
    "wire format"
  ],
  "categories": [
    "api-schemas",
    "realtime",
    "agent-events"
  ],
  "source_docs": [
    "01b92ca2e079328d"
  ],
  "backlinks": null,
  "word_count": 406,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`SystemEventData` is the wire-format contract for server-to-client push notifications in PocketPaw. Rather than sending raw dicts or ad-hoc JSON blobs over the SSE stream, every system event is serialised through this model, giving the front-end a stable shape to decode.

## Why SSE?

Server-Sent Events are a one-way HTTP/1.1 push channel. They suit agent runtimes well because the server continuously emits progress updates (tool calls starting, thinking states, errors) while the client holds a single long-lived GET connection. WebSockets would add unnecessary bidirectional complexity for a flow that is inherently server-driven.

## Field Design

```python
class SystemEventData(BaseModel):
    event_type: str
    content: str = ""
    metadata: dict = {}
```

- **`event_type`** — a required discriminator string (e.g. `"agent_thinking"`, `"tool_start"`, `"error"`). Keeping this as a plain `str` rather than an `Enum` lets the server introduce new event kinds without a schema migration.
- **`content`** — the human-readable or structured text payload. Defaults to an empty string so consumers can safely render it without null checks.
- **`metadata`** — an escape hatch for extra context that doesn't fit the typed fields. Tool names, token counts, session IDs, and similar supplementary data travel here. Using a plain `dict` intentionally avoids over-specifying the shape; different event types carry different metadata keys.

## Defensive Choices

All non-required fields carry safe defaults. This prevents `ValidationError` when older server versions emit events that lack newer optional fields — a common forward-compatibility concern in long-running SSE streams where the client might be a cached browser tab from a previous release.

The `from __future__ import annotations` import enables PEP 563 deferred evaluation, so forward references in type hints resolve at runtime without circular-import issues as the schema module grows.

## Integration Pattern

The emitter side constructs an instance, calls `.model_dump()`, serialises to JSON, and writes it to the SSE response. The receiver (usually Alpine.js or a browser `EventSource`) parses the JSON and dispatches on `event_type`. Having Pydantic validate on the way out means malformed events are caught server-side before reaching clients.

## Known Gaps

- `event_type` is an unconstrained string. There is no shared enum or registry of valid event types, so the front-end and back-end must stay in sync manually. A future `Literal` union or dedicated `EventType` enum would make exhaustive handling enforceable at the type-checker level.
- `metadata` is typed as `dict` with no value-type constraint (`dict[str, Any]` would be slightly more explicit). Downstream consumers must defensively key-check before accessing metadata fields.