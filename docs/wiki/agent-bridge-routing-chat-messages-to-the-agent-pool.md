---
{
  "title": "Agent Bridge: Routing Chat Messages to the Agent Pool",
  "summary": "The agent bridge connects incoming group-chat `message.sent` events to the PocketPaw agent pool, evaluating each agent's respond mode and streaming responses back over WebSocket. It decouples the chat send path from agent processing by dispatching all work to background tasks, preventing smart-mode LLM calls from blocking message delivery.",
  "concepts": [
    "agent pool",
    "respond_mode",
    "smart mode",
    "mention_only",
    "asyncio background task",
    "WebSocket streaming",
    "AgentStreamChunk",
    "ripple spec",
    "PocketService",
    "attachment forwarding",
    "event bus",
    "KnowledgeService",
    "Haiku relevance check",
    "stream throttling"
  ],
  "categories": [
    "agent runtime",
    "real-time messaging",
    "event handling",
    "cloud EE"
  ],
  "source_docs": [
    "c6287e5e79928885"
  ],
  "backlinks": null,
  "word_count": 644,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The agent bridge (`ee/cloud/shared/agent_bridge.py`) is the orchestration layer between the real-time chat event bus and the PocketPaw agent pool. When a user sends a message in a group channel, the bridge decides which agents should respond, runs each response, and streams results back to the client — all without blocking the sender's message acknowledgment.

## Why It Exists

Before this module, agent response logic lived inline on the chat send path. A `smart`-mode agent would call a cheap Haiku LLM to check relevance before deciding to respond. That call took 5–10 seconds, making the original sender wait seconds to see their own message acknowledged. The bridge fixes this by dispatching all agent processing to a detached `asyncio.Task`.

## Respond Mode Precedence

Each agent in a group has a `respond_mode` that gates whether it replies:

1. **`silent`** — never responds, even if explicitly mentioned.
2. **Explicit agent mention** — when a user @mentions one or more agents by type, only the named agents respond; `auto`-mode agents stay quiet to avoid multi-agent pile-ons.
3. **Directed replies** — a reply to a human suppresses all agents; a reply to a different agent also suppresses non-mentioned agents; a reply to this agent falls through to normal mode logic.
4. **Fallback modes** — `auto` always responds, `mention_only` never responds without a mention, `smart` fires a Haiku LLM call to judge relevance.

This precedence prevents reply threads from being hijacked by agents who were not part of the conversation.

## Attachment Forwarding

Prior to the 2026-04-19 change, channel agents received only the text content of a message. Attachment context (filename, MIME type, size) was only forwarded on the DM path. The bridge now calls `_augment_message_with_attachments()` to append an `Attached files:` block to the user prompt before calling `pool.run`. This mirrors the DM path shape so agents can reason about uploaded files without modifying the pool's call signature.

```python
user_message = _augment_message_with_attachments(user_message, attachments)
```

Missing attachment fields degrade gracefully — an entry with only a name still renders rather than silently dropping the attachment.

## Response Streaming and Throttling

Streaming chunks are emitted via WebSocket using `AgentStreamChunk` events. Without throttling, a verbose agent would emit one WebSocket message per LLM token, growing bandwidth usage O(n^2) with response length. The bridge coalesces chunks to at most one emit per 200ms:

```python
STREAM_CHUNK_THROTTLE_S = 0.2
if now - last_emit_ts >= STREAM_CHUNK_THROTTLE_S:
    await emit(AgentStreamChunk(...))
```

The final `AgentStreamEnd` event carries the authoritative full text, so the throttled intermediate chunks are a lossless UX optimization.

## Ripple Spec Parsing and Pocket Delegation

Agents can embed a JSON ripple spec (structured UI definition) inside their response text using a triple-backtick json block. The bridge detects this, parses it, strips it from the visible message text, and delegates pocket creation to `PocketService.create_from_ripple_spec()`. This decoupling was introduced to replace inline pocket creation logic that tightly coupled the bridge to the pockets domain.

## Concurrent Agent Safety

Each agent's response is its own `asyncio.Task`. The `AgentStreamEnd` event echoes the `temp_message_id` from the matching `AgentStreamStart`. Without this, a group with two concurrent agents would race on a `startsWith('agent-stream-')` lookup on the frontend, causing one stream to accidentally finalize the wrong placeholder row.

## Knowledge Context Injection

Before calling `pool.run`, the bridge queries `KnowledgeService.search_context()` to find relevant context from the agent's knowledge base. Failures are caught and logged at warning level — a knowledge search failure never aborts agent response delivery.

## Known Gaps

- Background tasks are tracked in a module-level `_background_tasks` set to prevent garbage collection, but there is no cap on concurrent background tasks. High-traffic groups could accumulate many in-flight tasks.
- The smart-mode relevance check creates and tears down a backend instance per call, which is wasteful. A pooled or cached Haiku instance would reduce overhead.
- `_run_agent_response` uses a 20-message history cap. There is no configurable window or summary strategy for long-running group conversations.