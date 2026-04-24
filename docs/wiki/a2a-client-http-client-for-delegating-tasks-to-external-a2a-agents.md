---
{
  "title": "A2A Client: HTTP Client for Delegating Tasks to External A2A Agents",
  "summary": "`A2AClient` is an async HTTP client that allows PocketPaw to communicate with external A2A-compatible agents. It supports blocking task submission, SSE streaming, task polling, cancellation, and Agent Card discovery — with connection sharing, TTL-cached Agent Cards, and transparent wrapping of direct-Message responses into the Task model.",
  "concepts": [
    "A2AClient",
    "httpx",
    "Agent Card",
    "TTL cache",
    "SSE streaming",
    "send_task",
    "send_task_stream",
    "cancel_task",
    "context manager",
    "direct Message wrapping",
    "A2A protocol"
  ],
  "categories": [
    "A2A protocol",
    "HTTP client",
    "agent runtime",
    "streaming"
  ],
  "source_docs": [
    "4a0a5f2aeaee03f7"
  ],
  "backlinks": null,
  "word_count": 422,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`A2AClient` handles all outbound A2A communication: discovering a remote agent's capabilities, sending tasks, receiving streamed responses, and cancelling in-flight work. It is built on `httpx.AsyncClient` and designed to be used both as a one-off caller and as a long-lived context manager.

## Context Manager vs. One-Off Usage

The client can be used in two modes:

```python
# Shared connection (recommended for multi-turn workflows)
async with A2AClient() as client:
    card = await client.get_agent_card(url)
    task = await client.send_task(url, params)

# One-off (each method opens+closes its own connection)
client = A2AClient()
task = await client.send_task(url, params)
```

The `_get_client()` context manager internally yields the shared client if one exists, or opens a temporary one otherwise. This avoids the latency of TCP handshakes on every call in multi-turn agentic workflows.

## Agent Card Caching

`get_agent_card` fetches `{base_url}/.well-known/agent.json` and validates it into an `AgentCard` model. Results are cached with a 60-second TTL:

```python
self._card_cache: dict[str, tuple[float, AgentCard]] = {}
self._card_cache_ttl = 60.0
```

The TTL prevents hammering the remote agent's discovery endpoint on every task in a batch while still catching capability updates within a reasonable window.

## Direct Message Response Wrapping

The A2A spec allows a remote agent to return either a `Task` object or a direct `Message`. `send_task` handles both:

```python
if "status" in data:
    return Task.model_validate(data)

# Otherwise treat as a direct Message response
message = A2AMessage.model_validate(data)
return Task(
    id=params.id,
    status=TaskStatus(state=TaskState.COMPLETED, message=message),
    history=[params.message, message],
)
```

Wrapping the message in a synthetic completed `Task` normalizes the return type so callers never need to handle two different shapes. Without this, every call site would need a union-type check.

## SSE Streaming

`send_task_stream` uses `httpx`'s streaming mode and yields the data payload of each SSE `data:` line:

```python
async for line in response.aiter_lines():
    if line.startswith("data:"):
        yield line[5:].strip()
```

The `_check_status` helper is used instead of `_handle_response` here because `_handle_response` reads `.content`, which is incompatible with streaming — it would buffer the entire response before returning.

## Error Handling Split

Two error helpers exist for a reason:
- `_handle_response`: reads `.content` (the response body) and raises on HTTP errors. Used for blocking calls.
- `_check_status`: only checks the status code, never reads the body. Used for streaming to avoid consuming the stream.

Calling `_handle_response` on a streaming response would block until the stream ends, defeating the purpose of streaming.

## Known Gaps

The `auth_headers` parameter is passed to `httpx.AsyncClient` at construction but there is no mechanism for per-request auth or token refresh. Long-lived clients with expiring JWTs would need to be recreated when tokens expire.