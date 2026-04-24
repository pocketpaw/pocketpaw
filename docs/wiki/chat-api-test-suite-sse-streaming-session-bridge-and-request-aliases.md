---
{
  "title": "Chat API Test Suite: SSE Streaming, Session Bridge, and Request Aliases",
  "summary": "Covers the full test surface of PocketPaw's `/api/v1/chat` router, including the `_APISessionBridge` async queue, SSE stream/stop endpoints, non-streaming send, media URL rewriting, and camelCase field alias handling. These tests exist to catch silent regressions in the real-time chat path that would break frontend consumers without an obvious error.",
  "concepts": [
    "SSE streaming",
    "_APISessionBridge",
    "asyncio.Queue",
    "session management",
    "_active_streams",
    "media URL resolution",
    "camelCase aliases",
    "Pydantic field aliases",
    "InboundMessage",
    "FastAPI TestClient",
    "event-stream content type"
  ],
  "categories": [
    "testing",
    "chat API",
    "real-time streaming",
    "API design",
    "test"
  ],
  "source_docs": [
    "ef27f6f9f2f22b48"
  ],
  "backlinks": null,
  "word_count": 606,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_api_chat.py` is the primary test file for PocketPaw's chat API layer (`pocketpaw.api.v1.chat`). The module exercises the runtime at multiple levels — from low-level queue internals to full HTTP round-trips — because the chat path is the most latency-sensitive and user-visible part of the system.

## _APISessionBridge

`TestAPISessionBridge` tests the internal async queue that bridges the HTTP layer to the agent runtime. The bridge holds an `asyncio.Queue` and allows the SSE stream handler to `put` data while a consumer task calls `get`. These tests validate that the bridge can be created cleanly and that data round-trips through `put`/`get` without corruption.

**Why it matters:** Without an isolated bridge per session, concurrent users would leak events across sessions. The bridge ensures each streaming request gets its own isolated queue.

## SSE Streaming Endpoint

`TestChatStream` validates `POST /api/v1/chat/stream`. It mocks the `_APISessionBridge` constructor and the downstream `send` function to return a canned async generator, then verifies the response carries `text/event-stream` content type. The inner `_load()` coroutine simulates the agent's response sequence.

**Failure it prevents:** If the SSE content type header is dropped, browsers stop treating the response as an event stream and the UI freezes waiting for a complete body that never arrives.

## Stop Endpoint

`TestChatStop` covers three stop scenarios:
- **No session_id** — expects a 422 validation error from FastAPI before the handler executes.
- **Non-existent session** — expects a 404; verifies the server doesn't crash trying to stop a session it doesn't track.
- **Active stream** — expects a 200 OK and confirms the stream is cleaned up from `_active_streams`.

These three cases together prevent the server from silently ignoring a stop request, which would leave orphaned agent tasks consuming resources.

## Non-Streaming Send

`TestChatSend` tests `POST /api/v1/chat`, the synchronous (non-streaming) send path. It validates that the endpoint assembles a complete response before returning, and that submitting an empty `content` field returns a 422 rather than forwarding a blank message to the agent.

## SSE Format Conformance

`TestSSEFormat` parses the raw SSE bytes returned by the stream endpoint and asserts each event follows `event: <type>
data: <json>

` — the exact format mandated by the SSE spec. Browsers silently discard events that deviate from this format, which would produce invisible data loss that is very hard to debug.

## Media URL Resolution

`TestSendMessageResolvesMedia` addresses a specific integration requirement: when a frontend uploads a file, it receives a temporary upload URL. Before the message is published to the internal bus, `_send_message` must rewrite those upload URLs to local filesystem paths so the agent receives a path it can actually read.

Two sub-tests cover the happy path (URLs are rewritten) and the no-media path (metadata is clean when no upload URLs are present). Both monkeypatch the bus `publish_inbound` to capture the outbound `InboundMessage` without touching the real agent runtime.

## CamelCase Field Aliases

`TestChatRequestAliases` exists because the `paw-enterprise` frontend posts JSON with camelCase keys (`sessionId`, `agentId`) while PocketPaw's Pydantic models use snake_case internally. Without explicit `alias` configuration on the request model, Pydantic silently drops unrecognised keys — the field arrives as `None` and the request either fails validation or routes to the wrong session.

Three alias tests verify: camelCase `sessionId` binds, snake_case `session_id` still binds, and camelCase `agentId` binds. These serve as a regression fence against future Pydantic model refactors that might accidentally drop alias configuration.

## Known Gaps

No explicit TODO or FIXME comments are surfaced in the AST extract. The `_load()` helper used in several test classes is an internal coroutine that simulates agent response; it is not exported and has no doc, which could make it harder to understand test intent during future maintenance.