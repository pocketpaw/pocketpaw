---
{
  "title": "A2A Protocol Server — Agent-to-Agent HTTP Endpoint for PocketPaw",
  "summary": "Implements the Agent-to-Agent (A2A) protocol server that exposes PocketPaw as an interoperable AI agent over HTTP. It provides a JSON-RPC 2.0 dispatcher, an Agent Card capability manifest, task submission endpoints, SSE streaming, and an in-memory task store with TTL-based expiry.",
  "concepts": [
    "A2A protocol",
    "Agent Card",
    "JSON-RPC 2.0",
    "SSE streaming",
    "task lifecycle",
    "message bus",
    "AgentLoop",
    "TaskState",
    "TTL expiry",
    "capability manifest",
    "push notifications",
    "A2ASessionBridge",
    "task ID validation"
  ],
  "categories": [
    "agent-runtime",
    "interoperability",
    "api",
    "streaming"
  ],
  "source_docs": [
    "f474c73136e34b29"
  ],
  "backlinks": null,
  "word_count": 541,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The A2A server (`pocketpaw/a2a/server.py`) makes PocketPaw a first-class participant in the emerging Agent-to-Agent interoperability ecosystem. External orchestrators, other AI agents, and developer tools that speak the A2A protocol can discover PocketPaw's capabilities, submit tasks, stream results, and cancel in-flight work — all without knowing anything about PocketPaw's internal architecture.

## Endpoints

| Path | Method | Purpose |
|------|--------|---------|
| `/.well-known/agent.json` | GET | Agent Card — capability manifest |
| `/.well-known/agent-card.json` | GET | Alias for spec-correct path |
| `/a2a` | POST | JSON-RPC 2.0 dispatcher |
| `/a2a/tasks/send` | POST | Submit a task, wait for completion |
| `/a2a/tasks/send/stream` | POST | Submit a task, receive SSE stream |
| `/a2a/tasks/{task_id}` | GET | Poll current task state |
| `/a2a/tasks/{task_id}/cancel` | POST | Cancel an in-flight task |

## Agent Card and Capability Advertising

`_build_agent_card()` constructs an `AgentCard` from live PocketPaw config. It lists skills (chat, coding assistant, file operations) and advertises streaming and push-notification support. The card is cached with a 60-second TTL via `_get_agent_card_cached()` — rebuilding on every request would add unnecessary latency and re-read config on each incoming call.

## Task Lifecycle and the In-Memory Store

Tasks progress through states defined by `TaskState`: `submitted → working → completed | failed | canceled`. The server tracks up to 1,000 tasks in `_tasks` dict with monotonic timestamps. `_prune_expired_tasks()` removes terminal tasks older than one hour (TTL = 3,600 seconds), preventing unbounded memory growth on long-running deployments. A regex `_TASK_ID_RE` validates incoming task IDs against `^[a-zA-Z0-9._\-]{1,128}$` — this stops path-traversal and injection attempts before they reach any downstream handler.

## Message Routing via the Internal Bus

Task execution never bypasses PocketPaw's existing security and middleware stack. `_dispatch_to_agent()` publishes an `InboundMessage` onto the internal message bus, exactly as the REST `/api/v1/chat` endpoint does. The `_A2ASessionBridge` subscribes to the bus's outbound stream and feeds `AgentEvent` objects into an `asyncio.Queue`, which the SSE generator then consumes. This design means A2A traffic gets rate limiting, injection scanning, PII redaction, and memory storage for free.

## SSE Streaming

`_core_message_stream()` is the shared implementation behind both `tasks/send/stream` (REST) and the `message/stream` JSON-RPC method. It yields JSON-RPC response dicts serialised as `data:` SSE frames. Each frame is either a `TaskStatusUpdateEvent` (state change) or a `TaskArtifactUpdateEvent` (incremental text chunk). The generator checks a `cancel_event` asyncio flag on every iteration so cancellations take effect within one polling cycle.

## JSON-RPC Dispatcher

`A2ADispatcher` routes method names (`message/send`, `message/stream`, `tasks/get`, `tasks/cancel`, `tasks/resubscribe`, `tasks/pushNotification/set`, `tasks/pushNotification/get`) to individual handler coroutines. Unrecognised methods return a standard JSON-RPC error. Push-notification handlers return `UNSUPPORTED_OPERATION` today — the scaffolding exists but webhook delivery is not yet implemented.

## Security Considerations

- Task IDs are validated with a strict allowlist regex before any lookup.
- A2A endpoints require the `a2a` OAuth scope via `require_scope` dependency.
- The feature can be disabled entirely via `a2a_enabled` config; `_check_a2a_enabled()` raises HTTP 404 when off.

## Known Gaps

- **In-memory task store**: The comment `# Phase 3 may persist` signals that task state is lost on process restart. Distributed or multi-process deployments will not share task state.
- **Push notifications**: `pushNotification/set` and `pushNotification/get` are registered but return `UNSUPPORTED_OPERATION`.
- **`tasks/resubscribe`**: Only replays current state; live streaming for already-active tasks requires the session bridge still be alive.
