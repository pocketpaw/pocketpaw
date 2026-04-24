---
{
  "title": "Dashboard Channels: Channel Adapter Lifecycle and REST Endpoints",
  "summary": "dashboard_channels.py manages the lifecycle of channel adapters (Discord, Slack, WhatsApp, custom webhooks) and exposes REST endpoints for querying status, saving config, toggling channels on/off, and handling inbound webhooks. Adapters are started and stopped at runtime without server restarts.",
  "concepts": [
    "channel adapters",
    "Discord",
    "Slack",
    "WhatsApp",
    "webhook management",
    "adapter lifecycle",
    "MessageBus",
    "hot-reconfiguration",
    "QR pairing",
    "extras installation"
  ],
  "categories": [
    "Dashboard",
    "Channel Management"
  ],
  "source_docs": [
    "2de0d8b1f2718134"
  ],
  "backlinks": null,
  "word_count": 506,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/dashboard_channels.py` handles everything related to communication channel adapters in PocketPaw's dashboard layer. Extracted from `dashboard.py`, it owns the adapter registry, lifecycle management, and all channel-related API endpoints. The design goal is hot-reconfiguration: a user can add a Discord bot token, click "Enable", and have the bot connect without restarting the server.

## Adapter Lifecycle

### _start_channel_adapter()

Each channel has a dedicated startup code path:

- **Discord** — requires `discord_bot_token`. Constructs a `DiscliAdapter` (note the unusual name — this is the internal Discord adapter class) with allow-lists for guilds, users, and channels. Starts it against the shared `MessageBus` and stores the instance in `_channel_adapters["discord"]`.
- **Slack** — requires both `slack_bot_token` and `slack_app_token` (Slack's Socket Mode needs both). Constructs `SlackAdapter` with optional channel allow-lists.
- **WhatsApp** — checks `whatsapp_mode` and branches between `cloud_api` (Meta's Business API) and `neonize` (WhatsApp Web automation). Returns `False` if no mode is selected.

The function returns `True` on success, `False` if required credentials are missing. This allows the dashboard to report "not configured" rather than "failed" to the UI.

### _stop_channel_adapter()

Calls `adapter.stop()` if the adapter is running, removes it from `_channel_adapters`, and returns whether the adapter was previously running. Stopping an adapter that isn't running is a no-op.

## Webhook System

PocketPaw supports generic inbound webhooks via named slots. Each slot has:
- A `webhook_name` (URL-safe identifier)
- An auto-generated secret (HMAC key for request verification)
- Optional payload routing config

Endpoints:
- `GET /api/webhooks` — lists all configured slots with their generated URLs
- `POST /api/webhooks/add` — creates a new slot with a UUID-based secret
- `POST /api/webhooks/remove` — removes a slot by name
- `POST /api/webhooks/regenerate-secret` — rotates the secret for a slot

The `POST /webhook/inbound/{webhook_name}` endpoint receives external HTTP calls, verifies the HMAC signature, and publishes the payload to the `MessageBus`.

## WhatsApp QR Pairing

`GET /api/whatsapp/qr` returns the current pairing QR code for the neonize (WhatsApp Web) mode. When a WhatsApp adapter starts in neonize mode, it generates a QR code that the user must scan with their phone to link the account. This endpoint allows the dashboard to display that QR in the UI.

## Extras Installation

`POST /api/extras/install` and `GET /api/extras/check` support optional dependency installation at runtime. For example, the WhatsApp neonize mode requires the `neonize` package, which is not installed by default. Users can install it from the dashboard without dropping to a terminal.

## Channel Status API

`GET /api/channels/status` returns a dict mapping each channel name to its configuration and runtime state, using the helper functions from `dashboard_state.py`:
- `_channel_is_configured()` — has required credentials
- `_channel_is_running()` — currently connected
- `_channel_autostart_enabled()` — will auto-start on next dashboard launch

`POST /api/channels/save` and `POST /api/channels/toggle` persist changes to `Settings` and optionally start/stop the adapter immediately.

## Known Gaps

- The `DiscliAdapter` naming is inconsistent with the `DiscordAdapter` name used elsewhere. This appears to be a typo that has propagated.
- There is no reconnection logic. If a Discord or Slack adapter drops its connection, it won't auto-reconnect without a manual toggle or server restart.