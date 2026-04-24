---
{
  "title": "Google Chat Channel Adapter — Webhook and Pub/Sub Modes",
  "summary": "GoogleChatAdapter connects PocketPaw to Google Workspace Chat via two modes: webhook (for setups where the PocketPaw dashboard receives HTTP POST events from Google) and pubsub (for server-side polling via Google Cloud Pub/Sub). Both modes normalise incoming events to InboundMessage on the bus and use the Google Chat REST API to send replies, with markdown converted to Google Chat's Card format.",
  "concepts": [
    "GoogleChatAdapter",
    "Google Chat",
    "webhook mode",
    "pubsub mode",
    "Google Cloud Pub/Sub",
    "service account",
    "markdown conversion",
    "space access control",
    "handle_webhook_message",
    "convert_markdown",
    "InboundMessage"
  ],
  "categories": [
    "channel-adapters",
    "google-chat",
    "google-workspace",
    "message-bus"
  ],
  "source_docs": [
    "0000000000000013"
  ],
  "backlinks": null,
  "word_count": 419,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Two Modes of Operation

Google Chat does not support a persistent WebSocket connection. PocketPaw integrates via one of two delivery mechanisms depending on the deployment environment:

### Webhook Mode
Google Chat pushes events to a configured HTTPS endpoint. In this mode, `GoogleChatAdapter` exposes a `handle_webhook_message(payload)` coroutine that the dashboard's FastAPI router calls when a POST arrives at `/webhook/gchat`. No background task is needed — Google drives the events. This mode requires a publicly accessible HTTPS URL, making it the natural choice for cloud deployments.

### Pub/Sub Mode
For environments without a public IP (e.g., a developer's laptop or an internal corporate server), Google Chat can publish events to a Cloud Pub/Sub topic. The adapter spawns a `_pubsub_loop()` background task that polls the subscription for new messages and acknowledges them after processing. This mode requires a Google service account and a Pub/Sub subscription, but avoids the need for ingress firewall rules.

## Authentication

`_init_credentials()` loads a Google service account key file and creates credentials scoped to the Chat and Pub/Sub APIs. The credentials are initialised lazily on `_on_start()` rather than at construction time so that the adapter can be instantiated and stopped without touching the network — useful in tests and in scenarios where credentials are injected after construction.

If credential initialisation fails, the adapter logs an error and returns from `_on_start()` without raising. This prevents a bad service account key from crashing the entire bot gateway on startup.

## Message Normalisation

Incoming events from both modes are normalised to `InboundMessage` with the Chat space ID as the `chat_id`. The sender's display name and email are extracted from the event payload and included in the message metadata so the agent can address the user by name.

## Markdown Conversion

Google Chat does not support CommonMark markdown. The adapter calls `convert_markdown()` from `pocketpaw.bus.format` to convert agent responses to Google Chat's supported formatting (bold via `*`, italic via `_`, code via `` ` ``). Without this conversion, markdown symbols would appear as literal characters in the Chat message.

## Space Access Control

`allowed_space_ids` filters incoming messages to a whitelist of Chat space IDs. Messages from spaces not on the list are silently dropped. An empty list means all spaces are allowed, which is the default for single-tenant deployments.

## Known Gaps

The Pub/Sub polling loop uses a fixed sleep interval between poll attempts. There is no exponential backoff on Pub/Sub API errors, so a temporary Google Cloud outage would generate a stream of error log lines rather than backing off gracefully.