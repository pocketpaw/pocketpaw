---
{
  "title": "Microsoft Teams Channel Adapter via Bot Framework SDK",
  "summary": "TeamsAdapter integrates PocketPaw with Microsoft Teams using the Bot Framework SDK, receiving messages via an internal aiohttp webhook server and sending replies through the Bot Framework's `send_activity` API. Streaming is emulated by editing a placeholder message with `update_activity()`.",
  "concepts": [
    "Bot Framework SDK",
    "botbuilder-core",
    "aiohttp webhook server",
    "App ID",
    "App Password",
    "JWT token validation",
    "update_activity",
    "turn context",
    "tenant allow-list",
    "ActivityTypes.MESSAGE",
    "Azure Bot Connector"
  ],
  "categories": [
    "channel-adapters",
    "messaging",
    "microsoft-teams",
    "enterprise"
  ],
  "source_docs": [
    "5b0aa10da8f94431"
  ],
  "backlinks": null,
  "word_count": 460,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

TeamsAdapter uses Microsoft's `botbuilder-core` and `botbuilder-integration-aiohttp` packages to participate in the Teams bot ecosystem. Unlike Socket Mode (Slack) or polling (Signal), Teams bots receive messages through HTTP POST callbacks from Azure's Bot Connector service. The adapter runs its own lightweight aiohttp server (separate from PocketPaw's main FastAPI server) to receive these webhook calls.

## Internal Webhook Server

`_on_start()` launches `_run_webhook_server()` as an asyncio task. This creates an `aiohttp.web.Application` listening on a configurable `webhook_port` (default 3978) at path `/api/messages/teams`. The server is purpose-built as a separate process listener so that Teams webhooks can be proxied via ngrok or Azure Bot registration without coupling to PocketPaw's existing FastAPI routes.

`_handle_webhook()` reads the raw request body, passes it to the Bot Framework adapter's `process()` method (which handles HMAC signature verification and activity parsing), and invokes `on_turn()` for each verified activity.

## Authentication

Bot Framework authentication uses Microsoft App ID and App Password credentials. The SDK's `BotFrameworkAdapterSettings` validates the JWT token that Azure Bot Service attaches to every inbound request. Requests with invalid or missing credentials are rejected at the framework level. The `allowed_tenant_ids` list provides an additional guard, dropping messages from Azure tenants not in the allow-list.

## Activity Processing

`on_turn()` delegates to `_process_activity()`, which filters for `ActivityTypes.MESSAGE` activities. The message text is extracted, media attachments are logged (full download not yet implemented), and an `InboundMessage` is published to the bus. The turn context is stored in `_contexts` keyed by `chat_id` so that `_send_text()` can call `turn_context.send_activity()` to reply.

## Streaming via Activity Updates

Teams does not support Slack-style live-edited messages natively, but `update_activity()` can update a previously sent activity. The adapter stores the `activity_id` from the first streamed response in `_stream_activities` and calls `update_activity()` on subsequent chunks, producing a live-typing effect. On `stream_end`, a final update is issued and the activity ID is cleared.

## Turn Context Lifecycle

Turn contexts cannot be held indefinitely — they are valid only within the scope of a Bot Framework turn. The adapter stores them in `_contexts` with the expectation that replies arrive within a reasonable window. Long-running agent responses may encounter expired context handles, resulting in a silent failure on the Teams side.

## Graceful Shutdown

`_on_stop()` cancels the webhook server task and shuts down the aiohttp runner, ensuring the port is released. Any pending stream activities are discarded.

## Known Gaps

- Attachment download from Teams messages is logged but not implemented — incoming files produce a log warning rather than being saved to disk.
- Turn contexts expire; a long-running agent response may fail silently if the Bot Framework turn window closes before the reply is sent.
- The adapter requires a public HTTPS endpoint (or ngrok tunnel) for Azure Bot Service to deliver webhook events — no fallback polling exists.