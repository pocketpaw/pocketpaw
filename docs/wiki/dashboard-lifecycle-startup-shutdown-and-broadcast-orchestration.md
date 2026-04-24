---
{
  "title": "Dashboard Lifecycle: Startup, Shutdown, and Broadcast Orchestration",
  "summary": "dashboard_lifecycle.py owns the `startup_event()` and `shutdown_event()` functions that initialize and tear down every PocketPaw subsystem in the correct order, plus broadcast helpers that push notifications, audit entries, and health updates to all connected WebSocket clients.",
  "concepts": [
    "startup sequence",
    "shutdown sequence",
    "broadcast helpers",
    "WebSocket broadcast",
    "reminder persistence",
    "health updates",
    "audit entries",
    "lifespan management",
    "MessageBus",
    "ProactiveDaemon"
  ],
  "categories": [
    "Dashboard",
    "Lifecycle Management"
  ],
  "source_docs": [
    "67bf23d4b49c2df2"
  ],
  "backlinks": null,
  "word_count": 440,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/dashboard_lifecycle.py` contains the orchestration logic for PocketPaw's startup and shutdown sequences, as well as the broadcast functions that push server-initiated messages to connected WebSocket clients. It was extracted from `dashboard.py` to isolate the "what happens when the server starts" concern from the routing and handler concerns.

## Startup Sequence

`startup_event()` runs during the FastAPI lifespan's startup phase. It initializes subsystems in dependency order:

1. **`ensure_project_directories()`** — creates `~/.pocketpaw/` and all required subdirectories. Must run first; everything else assumes these directories exist.
2. **`MessageBus`** — started before channel adapters connect to it.
3. **`AgentLoop`** — started to begin processing messages from the bus.
4. **Channel adapters** — Discord, Slack, WhatsApp adapters start if configured and auto-start is enabled.
5. **MCP server** — started if configured.
6. **Health engine** — initialized and wired to the broadcast callback.
7. **APScheduler** — started for health heartbeat jobs.
8. **ProactiveDaemon** — started last, since it depends on the bus, executor, and scheduler being ready.

Each step is wrapped in a broad `except Exception` with a `logger.warning()`. A failure in MCP startup, for example, doesn't abort the entire startup — the dashboard still comes up, just without MCP. This is intentional resilience.

## Shutdown Sequence

`shutdown_event()` runs during the lifespan's teardown phase (after the `yield`). It:

1. Stops all running channel adapters via `_stop_channel_adapter_fn` (injected to avoid circular imports).
2. Stops the ProactiveDaemon.
3. Stops the health engine scheduler.
4. Calls `cleanup_all()` on the rate limiter to flush state.

The `_stop_channel_adapter_fn` injection pattern exists because `dashboard_lifecycle.py` can't import `_stop_channel_adapter` directly from `dashboard_channels.py` without a circular import — both modules import from `dashboard_state.py`.

## Broadcast Helpers

### broadcast_reminder()

Sends a reminder to all connected WebSocket clients via three paths:

1. `ws_adapter.broadcast()` — the modern path via the `WebSocketAdapter`.
2. A legacy loop over `active_connections` — preserved as a fallback.
3. `notify()` from `pocketpaw.bus.notifier` — pushes to notification channels (Telegram, Discord).

It also persists the reminder as an `assistant` message in every active WebSocket session's memory. This ensures reminders survive page reloads and session switches — without this, a reminder received while the page was loading would be lost on next refresh.

### broadcast_intention()

Forwards intention execution result chunks to all WebSocket clients and calls the stream callback. Used by `ProactiveDaemon` when an intention fires.

### _broadcast_audit_entry() and _broadcast_health_update()

WebSocket-only broadcasts (no notification channel path). Audit entries are pushed as `{"type": "audit_entry", "entry": {...}}` events; health updates as `{"type": "health_update", "summary": {...}}`.

## Known Gaps

- The double-broadcast pattern in `broadcast_reminder()` (both `ws_adapter` and legacy `active_connections` loop) is a transitional artifact. The legacy path should be removed once the `WebSocketAdapter` is confirmed stable across all clients.