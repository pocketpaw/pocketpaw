---
{
  "title": "Agent Router: Registry-Based Backend Selection with Fallback",
  "summary": "`AgentRouter` is the runtime dispatcher that resolves a configured backend name through the registry, instantiates it, and routes all agent calls through it. It supports a user-configurable ordered list of fallback backends so that a misconfigured or unavailable primary backend degrades gracefully rather than crashing.",
  "concepts": [
    "AgentRouter",
    "backend selection",
    "fallback backends",
    "session_key",
    "lazy instantiation",
    "Settings",
    "async streaming",
    "get_backend_class",
    "error recovery",
    "active backend tracking"
  ],
  "categories": [
    "agents",
    "routing",
    "fault tolerance",
    "configuration"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 387,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`router.py` is the single point of contact between the rest of PocketPaw (API layer, WebSocket handler, CLI) and the pluggable agent backend ecosystem. It translates a `Settings.agent_backend` string into a live backend instance and owns the fallback logic when that backend is unavailable.

## Initialization and Backend Selection

`AgentRouter.__init__` immediately calls `_initialize_backend()`, which:

1. Reads `settings.agent_backend` (e.g., `"claude_agent_sdk"`)
2. Calls `get_backend_class(name)` from the registry
3. If the class is `None` (missing dependency or unknown name), logs a warning and hard-codes a fallback to `claude_agent_sdk`
4. Instantiates the class with the full `Settings` object

The `_active_backend_name` attribute tracks which backend is actually running, which may differ from the configured name after a fallback. This is surfaced to monitoring and health-check endpoints.

## Configurable Fallback Chain

`settings.fallback_backends` is a list of backend names the user can configure as an ordered fallback sequence. The router pre-caches fallback instances in `_fallback_instances` so that a switch during a live request does not incur cold-start latency.

`_get_fallback_backend(backend_name)` checks the cache first, then instantiates on miss. It handles `None` returns from the registry gracefully (logs and skips) so a misconfigured fallback list does not itself become a failure mode.

## Streaming Run Method

```python
async def run(message, *, system_prompt, history, session_key) -> AsyncIterator[AgentEvent]:
```

The `run` method delegates to the active backend's `run` method. The `session_key` parameter (absent in the legacy `AgentProtocol` signature) allows backends to isolate per-session state — particularly important for backends like OpenAI Agents SDK that maintain a thread/run lifecycle tied to a session identifier.

If the primary backend raises during `run`, the router catches the exception and attempts fallbacks in order. Each fallback that also raises is logged and skipped. If all backends fail, the router yields a single `AgentEvent(type="error", ...)` rather than letting the exception propagate to the WebSocket handler.

## Stop Passthrough

`stop()` forwards to the active backend's `stop()`. If the active backend is `None` (startup failure), stop is a no-op rather than raising `AttributeError`.

## Known Gaps

- The fallback logic is triggered on instantiation failure but the behavior on mid-stream backend errors (e.g., network loss after the first token) depends on each backend's own retry logic rather than a router-level fallback.
- `session_key` is passed to backends but not all backends use it — `AgentBackend` subclasses that do not support sessions silently ignore it.
