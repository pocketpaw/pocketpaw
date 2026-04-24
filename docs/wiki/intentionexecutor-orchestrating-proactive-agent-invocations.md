---
{
  "title": "IntentionExecutor: Orchestrating Proactive Agent Invocations",
  "summary": "IntentionExecutor ties together context gathering, prompt templating, and agent invocation into a single async pipeline that fires when an intention's trigger condition is met. It streams results back to connected clients via a configurable callback, supporting both WebSocket and Telegram delivery.",
  "concepts": [
    "IntentionExecutor",
    "intention execution",
    "agent invocation",
    "prompt templating",
    "stream callback",
    "AgentRouter",
    "stale session",
    "context injection",
    "async generator",
    "proactive daemon"
  ],
  "categories": [
    "Daemon",
    "Agent Runtime"
  ],
  "source_docs": [
    "d1b92106c9f47dcd"
  ],
  "backlinks": null,
  "word_count": 488,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`IntentionExecutor` in `src/pocketpaw/daemon/executor.py` is the execution engine that runs when a scheduled intention fires. Its job is to take an intention dict — which contains a prompt template and a list of context sources — and produce a stream of response chunks delivered to whoever is listening (a WebSocket client, a Telegram chat, etc.).

## Execution Pipeline

When `execute()` is called, it follows this sequence:

1. **Context gathering** — calls `ContextHub.gather()` with the intention's `context_sources` list. If the list is empty or absent, no context is injected.
2. **Stale-session variable injection** — if `session_meta` is provided (set when the trigger type is `stale_session`), the executor injects `{{session.title}}`, `{{session.idle_hours}}`, and `{{session.preview}}` into the context dict before templating. This allows an intention to say "You haven't touched your *{{session.title}}* session in {{session.idle_hours}} hours" with live data.
3. **Prompt templating** — calls `ContextHub.apply_template()` to substitute all `{{variable}}` placeholders in the intention's prompt string.
4. **Agent invocation** — passes the prepared prompt to `AgentRouter.stream()`, which dispatches to the configured backend (Claude, OpenAI, Ollama, etc.) and yields response chunks.
5. **Result streaming** — each chunk is yielded from `execute()` and also forwarded to the `stream_callback` if one is registered.

## Lazy Agent Router

The `AgentRouter` instance is created on first use via `_get_agent_router()`. This is intentional: the router reads from `Settings` which may not be fully populated at construction time (e.g., API keys loaded after config file parsing). Lazy initialization ensures the router sees the final settings state.

## Stream Callback Pattern

`set_stream_callback()` accepts an async callable with signature `(intention_id: str, chunk: dict) -> None`. The dashboard registers this callback during startup so WebSocket clients receive intention results in real time. The pattern keeps the executor decoupled from any specific delivery mechanism — the same executor can serve WebSocket, Telegram, or future channels without modification.

## execute_and_stream vs execute

`execute()` is an async generator that yields chunks. `execute_and_stream()` is a convenience wrapper that drives the generator and forwards every chunk to the registered `stream_callback`. The split exists because some callers (tests, CLI) want to consume the chunks directly, while the daemon's `_on_trigger` path wants fire-and-forget streaming.

## execute_by_id

`execute_by_id()` looks up an intention by ID from `IntentionStore` before delegating to `execute()`. This supports the "run now" dashboard action, where the user triggers an intention manually by ID rather than through a scheduled trigger.

## reset_agent

`reset_agent()` clears the cached `AgentRouter` instance, forcing a rebuild on next use. This is called after settings changes (e.g., a new API key is saved) so the router picks up the updated configuration without requiring a full daemon restart.

## Known Gaps

- There is no retry logic if agent invocation fails. A network timeout or API error will surface as an error chunk, but the executor will not retry automatically.
- The `session_meta` injection is specific to stale-session triggers; other trigger types that might benefit from metadata injection would require explicit extension of this path.