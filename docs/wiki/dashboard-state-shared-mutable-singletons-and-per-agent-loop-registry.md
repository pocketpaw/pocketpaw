---
{
  "title": "Dashboard State: Shared Mutable Singletons and Per-Agent Loop Registry",
  "summary": "dashboard_state.py is the shared state module for PocketPaw's dashboard, extracted from dashboard.py to break circular imports. It holds the global singletons (WebSocketAdapter, AgentLoop, StatusTracker), the per-agent loop registry with staleness detection, and channel inspection helpers.",
  "concepts": [
    "shared state",
    "singleton pattern",
    "WebSocketAdapter",
    "AgentLoop",
    "per-agent loop registry",
    "staleness detection",
    "circular import prevention",
    "MongoDB",
    "Beanie",
    "channel helpers"
  ],
  "categories": [
    "Dashboard",
    "State Management"
  ],
  "source_docs": [
    "c4a971b56dd0312f"
  ],
  "backlinks": null,
  "word_count": 444,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/dashboard_state.py` exists for one reason: circular imports. When `dashboard.py` was a monolith, all state lived there. As the codebase was split into `dashboard_auth.py`, `dashboard_channels.py`, `dashboard_lifecycle.py`, and `dashboard_ws.py`, each sub-module needed access to the same shared globals. Importing from `dashboard.py` would create circular dependencies. A dedicated state module breaks the cycle.

## Module-Level Singletons

Three objects are created at module import time:

- **`ws_adapter = WebSocketAdapter()`** — the WebSocket connection registry. Manages all active WS connections and provides a `broadcast()` method.
- **`agent_loop = AgentLoop()`** — the default agent processing loop. Consumes messages from the `MessageBus` and routes them to the configured LLM backend.
- **`status_tracker = StatusTracker()`** — tracks the current agent status (idle, processing, error).

The `AgentLoop` is immediately wired to the `CommandHandler` via `_get_cmd_handler().set_agent_loop(agent_loop)`. This allows `/kill` commands to cancel in-flight agent sessions without going through the bus.

## Per-Agent Loop Registry

For cloud deployments, each agent document in MongoDB may define a custom backend or persona. When a chat message targets a specific agent (via `/chat/stream?agent_id=...`), the system needs a dedicated `AgentLoop` configured for that agent, not the default singleton.

`get_agent_loop_for(agent_id)` manages this registry:

1. Acquires `_agent_loops_lock` (async lock to prevent concurrent builds for the same agent ID).
2. Loads the `Agent` document from MongoDB via Beanie.
3. If the doc's `updatedAt` has advanced past the cached stamp, discards the cached loop and rebuilds.
4. Caches `_NOT_FOUND` sentinel for agent IDs whose doc doesn't exist, preventing repeated MongoDB queries for deleted agents.
5. Falls back to the default `agent_loop` on any transient DB error.

The `_NOT_FOUND` sentinel is a custom class rather than `None` so the cache can distinguish "we looked and the doc doesn't exist" from "we haven't looked yet".

## Staleness Detection

The `_agent_loop_stamps` dict records the `Agent.updatedAt` value at the time each loop was built. On each request, `get_agent_loop_for()` compares the current doc's `updatedAt` to the stamp. If they differ, the cached loop is discarded. This means agent config changes (prompt, backend, parameters) take effect on the next request without restarting the server.

## Channel Inspection Helpers

Several small helper functions are defined here rather than in `dashboard_channels.py` to avoid circular imports:

- **`_channel_autostart_enabled()`** — checks the settings flag for a channel.
- **`_channel_is_configured()`** — checks that required credentials exist.
- **`_channel_is_running()`** — checks the `_channel_adapters` dict.
- **`_is_module_importable()`** — tries to import a module name and returns `True` on success. Used to detect whether optional extras (e.g., `neonize`) are installed.

## Known Gaps

- The per-agent loop registry has no eviction policy. Over time, a deployment with many agents would accumulate cached loops in memory indefinitely. A TTL or LRU cache would bound memory growth.