---
{
  "title": "Agent Status API — SSE Streaming and Debounced Snapshot Polling",
  "summary": "The agent status router exposes real-time agent state — global status plus per-session breakdowns — through both a polling REST endpoint and a Server-Sent Events stream. It uses fingerprint-based change detection and a 1-second debounce to avoid flooding SSE clients with identical snapshots during high-frequency tool execution.",
  "concepts": [
    "agent status",
    "SSE",
    "Server-Sent Events",
    "fingerprint debounce",
    "AgentStatusResponse",
    "status API key",
    "session state",
    "tool_name",
    "polling",
    "streaming",
    "config caching"
  ],
  "categories": [
    "API",
    "Real-time",
    "Monitoring"
  ],
  "source_docs": [
    "d0f847ba667f6b0f"
  ],
  "backlinks": null,
  "word_count": 408,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`agent_status.py` serves the `/agent/status` family of endpoints that give dashboards and monitoring tools a live window into the PocketPaw agent runtime. The data model mirrors `AgentStatusResponse`: a top-level `global.state` field paired with a list of active sessions, each carrying `session_key`, `state`, and `tool_name`.

## Two Access Modes: Polling and Streaming

The router exposes two consumption patterns:

1. **`GET /agent/status`** — a standard JSON snapshot, suitable for dashboards that poll on a timer.
2. **`GET /agent/status/stream`** — a Server-Sent Events stream that pushes updates only when the snapshot actually changes.

The SSE approach exists because the agent's state can change dozens of times per second during tool chains. Sending every raw status event would saturate HTTP connections and force clients to do expensive DOM updates on unchanged data.

## Fingerprint-Based Debouncing

Change detection is handled by `_snapshot_fingerprint(snap)`, which extracts only the stable, semantically meaningful fields:

```python
def _snapshot_fingerprint(snap: dict) -> tuple:
    global_state = snap["global"]["state"]
    sessions = tuple(
        (s["session_key"], s["state"], s["tool_name"]) for s in snap.get("sessions", [])
    )
    return (global_state, sessions)
```

Timing fields (timestamps, durations) are deliberately excluded. This means a snapshot where only the elapsed-time field changed will not trigger an SSE push — the client only receives a new event when something a user would actually care about has changed.

Combined with `_DEBOUNCE_MS = 1000`, the stream waits at least one second between identical-fingerprint snapshots before emitting, smoothing out rapid oscillations (e.g., an agent toggling between `running` and `idle` within a single tool call).

## Optional Status API Key

Not all deployments want the status endpoint fully public. `_get_status_api_key()` reads the configured key from `Settings` and caches it on the function object after the first call — avoiding repeated config file reads on every status poll. `_check_status_key()` then validates either a query parameter (`?key=...`) or an `x-status-key` header, and returns 403 if the provided value doesn't match.

The guard is fully optional: if no key is configured, all requests are allowed through. This keeps simple self-hosted deployments frictionless while giving enterprise operators a lightweight gate.

## Why Cached Config?

The `_get_status_api_key._value` attribute trick avoids importing the full `Settings` object on every request. In PocketPaw, `Settings.load()` reads from disk; calling it on every status poll would cause measurable latency on busy dashboards.

## Known Gaps

No explicit TODOs or FIXMEs in the source. The 1-second debounce constant (`_DEBOUNCE_MS`) is hard-coded and not exposed as a setting, which could be limiting for deployments that need finer-grained real-time feedback.