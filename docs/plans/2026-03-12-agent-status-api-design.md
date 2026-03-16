# Agent Status API & CLI Design

**Date:** 2026-03-12
**Purpose:** Public API endpoint, SSE stream, and CLI command to expose real-time agent state for external integrations (stream decks, LED indicators, desktop widgets).

---

## API Endpoint

### `GET /api/v1/agent/status`

**Auth:** Optional `X-Status-Key` header checked against `POCKETPAW_STATUS_API_KEY` config. If the config value is empty or unset, all requests pass through.

**Response:**

```json
{
  "global": {
    "state": "active",
    "active_sessions": 2,
    "max_concurrent": 5,
    "uptime_seconds": 3842
  },
  "sessions": [
    {
      "session_key": "websocket:abc123",
      "session_id": "abc123",
      "channel": "websocket",
      "title": "Fix auth bug",
      "state": "tool_running",
      "tool_name": "bash",
      "duration_seconds": 12,
      "token_usage": { "input": 1200, "output": 340 },
      "error_message": null
    }
  ]
}
```

**Global states:** `idle` (nothing running), `active` (at least one session working), `degraded` (any session in error state).

**Session states:** `thinking`, `tool_running`, `streaming`, `waiting_for_user`, `error`.

Only active sessions appear in `sessions[]`. Empty array when idle.

`error` state persists for ~30 seconds or until the next message in that session.

---

## SSE Stream

### `GET /api/v1/agent/status/stream`

**Auth:** `X-Status-Key` header or `?key=` query param for clients that cannot set headers.

Pushes a full snapshot on every state change:

```
event: status
data: {"global": {...}, "sessions": [...]}
```

- Full snapshot on initial connection (no need to poll first).
- Full snapshot per event (not diffs), so consumers are stateless.
- Debounced at ~200ms to avoid flooding during rapid tool sequences.

---

## CLI Command

### `pocketpaw status`

Human-readable table:

```
PocketPaw Status
  State:    active
  Sessions: 2 / 5
  Uptime:   1h 4m 2s

Active Sessions
  SESSION        CHANNEL     STATE          TOOL     DURATION
  Fix auth bug   websocket   tool_running   bash     12s
  Explain code   discord     thinking       -        4s
```

### `pocketpaw status --json`

Raw JSON, same shape as API response.

### `pocketpaw status --watch [INTERVAL]`

Refreshes and redraws every N seconds (default 2). `--watch 5` for 5-second interval.

CLI hits `http://localhost:8888/api/v1/agent/status` internally. Sends `X-Status-Key` from `POCKETPAW_STATUS_API_KEY` env var if set.

---

## Implementation

### New files

- `src/pocketpaw/status.py` - `StatusTracker` class. Bus subscriber that listens to `thinking`, `tool_start`, `tool_result`, `stream_end`, `ask_user_question`, `error` events. Maintains an in-memory dict of per-session state. Exposes `snapshot()` method for endpoints. Cleans up error states after 30 seconds.
- `src/pocketpaw/api/v1/agent_status.py` - FastAPI router with GET polling endpoint and GET SSE stream. Auth dependency checks `X-Status-Key`.

### Modified files

- `src/pocketpaw/api/v1/__init__.py` - register the new router in `_V1_ROUTERS`
- `src/pocketpaw/__main__.py` - add `status` subcommand with `--json` and `--watch` flags
- `src/pocketpaw/config.py` - add `status_api_key: str = ""` setting
- `src/pocketpaw/dashboard_state.py` - instantiate `StatusTracker` and subscribe to bus

### Auth middleware

Simple FastAPI dependency: compare `X-Status-Key` header to `config.status_api_key`. If config value is empty, skip check entirely.

### State tracking approach

`StatusTracker` subscribes to the message bus rather than reaching into `AgentLoop._active_tasks` private internals. It reacts to the same event stream the frontend already consumes, keeping the coupling loose.
