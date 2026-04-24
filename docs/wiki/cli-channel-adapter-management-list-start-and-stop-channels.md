---
{
  "title": "CLI Channel Adapter Management: List, Start, and Stop Channels",
  "summary": "The `channels` CLI command provides operators with a unified interface to inspect and control PocketPaw's eight supported messaging channel adapters — Discord, Slack, WhatsApp, Telegram, Signal, Matrix, Teams, and Google Chat. It separates read operations (listing configuration status) from write operations (toggling adapters via the dashboard REST API) to avoid side effects during inspection.",
  "concepts": [
    "channel adapters",
    "CLI commands",
    "Discord",
    "Slack",
    "Telegram",
    "WhatsApp",
    "REST API",
    "httpx",
    "autostart",
    "configuration check",
    "ANSI output",
    "channel toggle"
  ],
  "categories": [
    "CLI",
    "Channel Management"
  ],
  "source_docs": [
    "380fc8be2adfc993"
  ],
  "backlinks": null,
  "word_count": 596,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/cli/channels.py` implements the `pocketpaw channels` subcommand. It serves as the control surface for channel adapter lifecycle management: operators can see which channels are configured and set for autostart, and can start or stop any channel while PocketPaw is running.

## The `_ALL_CHANNELS` Registry

A module-level list defines every supported channel adapter:

```python
_ALL_CHANNELS = [
    "discord", "slack", "whatsapp", "telegram",
    "signal", "matrix", "teams", "google_chat",
]
```

This registry serves two purposes. First, it drives the listing loop in `_list_channels`, so every adapter is always shown even if unconfigured — the operator never has to guess what names are valid. Second, it acts as an allowlist in `_toggle_channel`: any channel name not in the list is rejected immediately, preventing a user from accidentally hitting an internal route with an arbitrary string.

## Listing Channels: `_list_channels`

This function reads `Settings` via a deferred import inside the function body. The deferred import is intentional — it avoids importing the full config stack at module load time, keeping CLI startup fast. For each channel, it calls `_is_configured` and `_get_autostart` to derive two boolean flags, then renders them in a color-coded table using ANSI helpers from `pocketpaw.cli.utils`.

The `as_json` flag switches the output to machine-readable JSON, enabling scripting and CI pipelines to consume channel status without parsing ANSI sequences.

## Toggling Channels via REST: `_toggle_channel`

Starting or stopping a channel is intentionally delegated to the running PocketPaw dashboard over HTTP rather than implemented inline. This design decision reflects the runtime architecture: channel adapters are long-lived async services managed by the dashboard process. The CLI process is a separate, short-lived child; it cannot directly control the event loop of the parent server.

The function posts to `http://localhost:{port}/api/channels/toggle` with a JSON body `{"channel": ..., "enable": ...}`. The `httpx` import is deferred inside the function — again to keep cold-start fast for the common list path.

Error handling covers three distinct failure modes:

- **`httpx.ConnectError`** — the dashboard is not running. The message explicitly tells the operator to check if the dashboard is up, rather than surfacing a raw connection refused error.
- **`httpx.HTTPStatusError`** — the dashboard returned a 4xx/5xx. The raw status code and response body are shown so the operator can diagnose API-level failures.
- **Generic `Exception`** — any unexpected error (timeout, SSL, etc.) is caught and printed without crashing the CLI process.

Response parsing handles two success signals: `status == "ok"` and `running == enable`. The dual check exists because API response shapes can evolve; this makes the toggle logic forward-compatible with minor schema changes.

## Configuration Checks: `_is_configured` and `_get_autostart`

`_is_configured` maps each channel name to its primary required credential field. It uses `getattr` with `None` as a default so that missing fields (e.g., if a future Settings version renames a field) return `False` rather than raising `AttributeError`.

`_get_autostart` follows the naming convention `{channel}_autostart` and also uses `getattr` with `False` as the default. This makes all channels appear as non-autostarting in the list output even if a future channel is added to `_ALL_CHANNELS` before its corresponding Settings field is defined.

## Known Gaps

- **Running status not shown**: The list view shows `configured` and `autostart` but does not indicate whether a channel is currently running. A `running` column would require an additional API call to the dashboard. This is likely a deliberate trade-off to keep `pocketpaw channels` usable even when the dashboard is offline.
- **Port is hardcoded to 8888 by default**: There is no mechanism to auto-discover the dashboard port. If the user starts PocketPaw on a non-default port, they must pass `--port` explicitly on every toggle command.
