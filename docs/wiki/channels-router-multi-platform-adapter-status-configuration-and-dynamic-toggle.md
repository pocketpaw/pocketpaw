---
{
  "title": "Channels Router — Multi-Platform Adapter Status, Configuration, and Dynamic Toggle",
  "summary": "The channels router manages PocketPaw's suite of messaging adapters — Discord, Slack, WhatsApp, Telegram, Signal, Matrix, Teams, and Google Chat — through a unified REST interface. It allows the dashboard to inspect adapter state, save credentials, start or stop adapters at runtime without restarting the server, retrieve WhatsApp QR codes, and install optional channel dependencies on demand.",
  "concepts": [
    "channel adapter",
    "Discord",
    "Slack",
    "WhatsApp",
    "Telegram",
    "Signal",
    "Matrix",
    "Teams",
    "Google Chat",
    "dynamic toggle",
    "neonize",
    "autostart",
    "channel status",
    "install extras"
  ],
  "categories": [
    "API",
    "Channels",
    "Integration"
  ],
  "source_docs": [
    "7d6517540231e020"
  ],
  "backlinks": null,
  "word_count": 434,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw connects AI agents to multiple messaging platforms through channel adapters. The channels router provides the control plane for these adapters: it is the API layer that the dashboard calls to configure, start, stop, and inspect every supported channel in a running instance.

## Unified Status Across Eight Channels

`get_channels_status()` iterates through a fixed tuple of supported channel names and returns three boolean flags per channel: `configured` (credentials are present), `running` (the adapter process is active), and `autostart` (the adapter starts automatically on server restart). The state is read from the `Settings` object and the live adapter registry.

WhatsApp gets extra fields because it supports two operating modes (`neonize` vs. `whatsapp-web.js`), and Discord exposes bot-name and activity-status fields used by the dashboard's channel card. These per-channel exceptions are hard-coded in the status response rather than pushed into a generic plugin interface.

## Dynamic Adapter Toggle

`toggle_channel` starts or stops an adapter at runtime by delegating to `start_channel()` / `stop_channel()` helpers. This prevents the common DevOps pattern of restarting the entire server to activate a newly configured channel — critical when the agent is mid-conversation on another channel and a restart would interrupt active sessions.

## Save Configuration Flow

`save_channel_config` persists token and configuration data for a channel. Because credentials differ per platform (a bot token for Discord, an API key + secret for others, phone number for WhatsApp), the request body is a generic dict passed to the config layer, which handles platform-specific validation downstream.

## WhatsApp QR Code Endpoint

WhatsApp's `neonize`-based integration requires a QR code scan to pair the server's phone number. `get_whatsapp_qr()` fetches the current QR image from the running WhatsApp adapter. The import path was fixed in the 2026-02-25 update after the WhatsApp module was reorganized.

## `install_extras` Error Handling Decision

When a channel's optional dependency (e.g., `neonize` for WhatsApp) is not installed, `install_extras` attempts installation and **always returns HTTP 200 with a JSON body** — even on failure. The changelog explains why:

> revert install_extras to return error JSON (200) instead of HTTP 500 to avoid breaking dashboard JS

The dashboard's fetch handler treated any non-200 response as a network error and swallowed the error detail. By keeping the status code at 200 and embedding the error in the JSON body, the dashboard can render the failure message properly.

## Known Gaps

The `autostart` flag is read from `Settings` but the toggle endpoint doesn't expose a way to persist the autostart preference — only the live state changes. A channel toggled on manually will not survive a server restart unless the user separately configures autostart in settings.