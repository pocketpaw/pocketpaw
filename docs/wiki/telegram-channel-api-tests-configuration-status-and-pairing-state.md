---
{
  "title": "Telegram Channel API Tests — Configuration Status and Pairing State",
  "summary": "This test module covers the two read-only Telegram status endpoints: `/api/v1/telegram/status` (reports whether a bot token and allowed user ID are configured) and `/api/v1/telegram/pairing-status` (reports live pairing state from a dashboard-level in-memory dict). Both endpoints inform the dashboard whether the Telegram channel is ready to receive messages.",
  "concepts": [
    "Telegram channel adapter",
    "bot token configuration",
    "pairing state",
    "GET /api/v1/telegram/status",
    "GET /api/v1/telegram/pairing-status",
    "_telegram_pairing_state",
    "channel setup",
    "allowed_user_id",
    "dashboard integration",
    "in-memory state patching"
  ],
  "categories": [
    "testing",
    "channel adapters",
    "API",
    "Telegram integration",
    "test"
  ],
  "source_docs": [
    "8d0368080a1f064b"
  ],
  "backlinks": null,
  "word_count": 636,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_api_v1_telegram.py` tests the Telegram integration status surface of PocketPaw's v1 API. PocketPaw supports Telegram as a channel adapter, allowing users to chat with their AI agent via a Telegram bot. Before the bot can function, two things must be true: a `telegram_bot_token` must be stored in settings, and a specific `allowed_user_id` must be configured to gate who can send messages to the bot.

The file covers the distinction between *configuration* (token and user ID stored in persistent settings) and *pairing* (an active runtime handshake between the dashboard and a Telegram user). These are different states, and the API surfaces them separately so the dashboard UI can show granular setup progress.

## `TestTelegramStatus`

`GET /api/v1/telegram/status` answers the question: "Is the bot configured at all?"

**`test_status_configured`** patches `Settings.load` to return a settings mock with a non-empty `telegram_bot_token` and a non-null `allowed_user_id`. The expected response is `{"configured": true, "user_id": 12345}`. The user ID is exposed so the dashboard can show which Telegram user is authorized, helping the owner confirm they configured the correct account.

**`test_status_not_configured`** patches settings with an empty token string and `allowed_user_id = None`. The response must be `{"configured": false, "user_id": null}`. This case is common during initial setup and must return HTTP 200 (not 404 or 500), because the absence of configuration is a valid informational state, not an error. If the endpoint returned 500 for unconfigured state, the dashboard would have to treat a 500 as "not configured yet" rather than a real server error — a semantic ambiguity that makes error handling harder.

## `TestTelegramPairingStatus`

`GET /api/v1/telegram/pairing-status` answers the question: "Has the user completed the Telegram pairing flow right now?"

Pairing is an ephemeral in-memory state managed by `pocketpaw.dashboard._telegram_pairing_state`, a module-level dict. The test patches this dict directly (using `@patch` on the module-level name) rather than patching a function, which is the correct approach when the production code reads the dict by reference at call time.

**`test_not_paired`** patches the state to `{"paired": False, "user_id": None}` and asserts the response contains `"paired": false`. This is the state before any user initiates the pairing flow.

**`test_paired`** patches the state to `{"paired": True, "user_id": 99999, "temp_bot_app": None}` and asserts `"paired": true` and `"user_id": 99999`. The `temp_bot_app: None` field in the patch reflects that the temporary bot application used during the pairing handshake has already been cleaned up (it is a transient object that exists only during the pairing conversation and is nulled out afterward).

## Why Two Separate Endpoints?

Configuration and pairing serve different phases of setup:

- A user might have the token configured but not yet have run the pairing wizard. `status` would return `configured: true` while `pairing-status` returns `paired: false`.
- After pairing, the runtime `_telegram_pairing_state` dict is updated by the dashboard; no settings write is needed. So `pairing-status` reads live memory while `status` reads the persistent config file.

Merging these into one endpoint would require the dashboard to interpret a combined state machine with four possible combinations, and would complicate the separation of concerns between the pairing wizard UI component and the channel configuration form.

## Known Gaps

- **Pairing initiation and teardown** — the test file covers only the status read endpoints. The endpoints that *start* the pairing flow or handle the Telegram webhook callback during pairing are not tested here, suggesting they are either covered elsewhere or not yet tested.
- **Token format validation** — `test_status_configured` uses `"123:ABC"` as a bot token, but there is no test that verifies a malformed token (e.g., just `"ABC"` without the numeric bot ID prefix) causes the configured field to return `false` or triggers a warning.
- **Thread safety of `_telegram_pairing_state`** — the in-memory dict is patched in tests, but there is no test for concurrent reads during an active pairing, which could produce inconsistent state if multiple dashboard WebSocket clients poll simultaneously.
