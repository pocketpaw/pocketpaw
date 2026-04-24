---
{
  "title": "Generic Inbound Webhook Adapter with Sync and Async Modes",
  "summary": "WebhookAdapter allows any external service — GitHub, Zapier, n8n, Home Assistant, cron scripts — to push events into PocketPaw via HTTP POST. It supports both fire-and-forget (async) mode and a synchronous request-response mode where the HTTP call blocks until the agent produces a reply.",
  "concepts": [
    "webhook",
    "WebhookSlotConfig",
    "async mode",
    "sync mode",
    "asyncio.Future",
    "request-response",
    "stream buffering",
    "HMAC verification",
    "fire-and-forget",
    "BaseChannelAdapter",
    "n8n",
    "Zapier",
    "Home Assistant"
  ],
  "categories": [
    "channel-adapters",
    "integrations",
    "webhook",
    "http"
  ],
  "source_docs": [
    "eafd0fdc9ab8cba6"
  ],
  "backlinks": null,
  "word_count": 485,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

WebhookAdapter is architecturally distinct from all other channel adapters: it does not poll, maintain a persistent connection, or manage its own transport. Instead, it acts as a passive receiver — PocketPaw's dashboard HTTP layer calls `handle_webhook()` for each incoming request, and the adapter converts the payload into a bus `InboundMessage`. This design keeps transport concerns (HTTP parsing, routing, HMAC verification) in the web layer while the adapter focuses on message semantics.

## Webhook Slot Configuration

Each registered webhook endpoint is described by a `WebhookSlotConfig` dataclass with `name`, `secret`, `description`, and `sync_timeout` fields. The `name` is included in every inbound message's metadata as `webhook_name`, allowing agents to distinguish between multiple registered webhooks (e.g., a GitHub webhook vs. a Home Assistant automation webhook). HMAC secret verification happens in the HTTP layer before `handle_webhook()` is called.

## Async Mode (Fire-and-Forget)

In the default async mode, `handle_webhook()` publishes the inbound message to the bus and returns `None` immediately. The HTTP handler can return a `202 Accepted` response without waiting for the agent to process the event. This is the correct behavior for event-driven integrations (e.g., a deployment notification) where the caller does not need a response.

## Sync Mode (Request-Response)

When `sync=True`, `handle_webhook()` registers an `asyncio.Future` in `_pending` keyed by `request_id` before publishing the inbound message. The `send()` method watches for outbound messages addressed to that `request_id` and resolves the future with the agent's response text.

Streaming responses are handled correctly: `is_stream_chunk` payloads are accumulated in `_buffers[request_id]`; the complete concatenated text is set on the future only when `is_stream_end` arrives. Non-streaming responses resolve the future immediately.

The caller then awaits `asyncio.wait_for(fut, timeout=slot.sync_timeout)`. On timeout, the pending future and buffer are cleaned up and `None` is returned, allowing the HTTP layer to respond with a timeout error.

## Payload Format

The standard payload shape is `{"content": "...", "sender": "...", "metadata": {...}, "media_urls": [...]}`. The `content` field is extracted directly. If absent, the entire JSON body is serialized as the content — this raw fallback supports simple services that POST arbitrary JSON without adhering to PocketPaw's schema.

`media_urls` is a list of URLs that the adapter downloads via `MediaDownloader.download_url()` and appends as local file paths to the `InboundMessage.media` list.

## No Outbound Capability

`send()` only resolves pending sync futures — it never writes to a socket. This is intentional: webhook integrations that need to push data back to the calling service should use the `notify()` utility or a dedicated outbound adapter. The one-way nature keeps the adapter simple and stateless for async use cases.

## Known Gaps

- HMAC verification of the `secret` field is delegated to the HTTP layer; the adapter itself does not re-validate signatures. A misconfigured route could bypass this check.
- Sync mode with very long agent runs can exhaust HTTP keep-alive windows on the caller's side before the timeout fires on PocketPaw's side.
- There is no per-slot rate limiting to prevent webhook flooding.