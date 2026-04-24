---
{
  "title": "Channels API Tests: Status, Config Save, Toggle, and Extras Check",
  "summary": "This test file covers PocketPaw's `/api/v1/channels` router, which exposes status, configuration, and lifecycle management for messaging channel adapters such as Discord, Slack, WhatsApp, Telegram, Signal, Matrix, Teams, and Google Chat. It also covers `/extras/check`, the endpoint that detects whether optional channel dependencies are installed.",
  "concepts": [
    "channel adapter",
    "dashboard state",
    "channel status",
    "autostart",
    "WhatsApp mode",
    "channel toggle",
    "extras check",
    "module-level side effects",
    "optional dependency detection",
    "messaging platforms"
  ],
  "categories": [
    "channel adapters",
    "API",
    "testing",
    "dashboard",
    "test"
  ],
  "source_docs": [
    "29aecc698213028a"
  ],
  "backlinks": null,
  "word_count": 502,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw connects to external messaging platforms through channel adapters. The channels API lets the dashboard read the current state of each adapter (configured, running, autostart-enabled), save credentials, start or stop adapters, and check for missing optional libraries. This test file exercises the full surface of that API.

## Module-Level Side-Effect Import

A notable detail at the top of the file:

```python
import pocketpaw.dashboard  # noqa: F401 — force module-level side effects
```

The comment explains the intent: importing `dashboard` triggers module-level registration that the channel router depends on at runtime. Without this import, the router's internal state may be uninitialised in the test process, causing lookups to silently fail. The `# noqa: F401` suppresses the linter warning for an "unused" import.

## Channel Status (`GET /channels/status`)

`TestChannelsStatus` patches three dashboard state functions and the settings loader to exercise the status endpoint:

- **All channels present**: The response dict must contain all eight expected channel keys. If a new channel adapter is added to the codebase without a matching entry in the status handler, this test will catch the regression.
- **Required status fields**: Each channel entry must have `configured`, `running`, and `autostart` fields. The dashboard UI renders these as badges; a missing field would cause a JavaScript null-reference error.
- **WhatsApp mode**: WhatsApp has an additional `mode` field (`"personal"` or `"business"`) because it has two distinct API integration paths. This test confirms the field propagates from settings to the API response.

## Save Channel Config (`POST /channels/save`)

`TestSaveChannelConfig` verifies that saving a valid channel's credentials calls `settings.save()` and that submitting an unknown channel name returns 400. The 400 prevents silent no-ops where a typo in the channel name causes credentials to be discarded without any error.

## Toggle Channel (`POST /channels/toggle`)

`TestToggleChannel` covers three error paths:

- **Unknown channel**: Returns 400.
- **Start while already running**: Returns 200 with an `error` field containing "already running". This is an idempotency guard — attempting to start a channel that is already active is not a fatal error, but the client should be informed so it can update its UI state.
- **Invalid action**: Any action string other than `"start"` or `"stop"` returns 400.

The `test_start_already_running` test uses direct monkey-patching of the `pocketpaw.dashboard` module attributes rather than `patch()`, because the channels router imports `_channel_is_running` directly from `dashboard` inside the function body. The test saves and restores the originals in a `try/finally` block to avoid state leakage across tests.

## Extras Check (`GET /extras/check`)

`TestExtrasCheck` verifies the endpoint that reports whether optional channel dependencies (e.g. `discord.py`) are installed:

- **Installed**: When `_is_module_importable` returns `True`, the response has `installed: true`.
- **No deps needed**: A channel not present in `_CHANNEL_DEPS` is always considered installed (no optional dependency required), returning `installed: true`.

## Known Gaps

No `TODO` or `FIXME` markers are present. The test suite does not cover the `stop` action in `TestToggleChannel`, nor does it test starting a channel that is not yet configured (which may return a different error from starting an unknown channel).