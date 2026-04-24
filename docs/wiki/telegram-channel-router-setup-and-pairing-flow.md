---
{
  "title": "Telegram Channel Router: Setup and Pairing Flow",
  "summary": "Implements the three REST endpoints that drive Telegram bot pairing: checking current configuration status, initiating the pairing flow with a bot token, and polling for pairing completion. The router wraps the shared pairing state dict from the dashboard module, adding token validation and graceful handling of missing optional dependencies.",
  "concepts": [
    "Telegram bot",
    "pairing flow",
    "bot token validation",
    "QR code",
    "deep link",
    "polling",
    "python-telegram-bot",
    "dashboard state",
    "503 Service Unavailable",
    "channel setup"
  ],
  "categories": [
    "api",
    "telegram",
    "channel-integration",
    "authentication"
  ],
  "source_docs": [
    "cdd3de7d4af3fef7"
  ],
  "backlinks": null,
  "word_count": 503,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Telegram Channel Router: Setup and Pairing Flow

The Telegram router exposes the v1 REST interface for connecting PocketPaw to a Telegram bot. The actual pairing orchestration — temporary bot lifecycle, QR code generation, user confirmation — lives in `dashboard.py`, and this router acts as a thin, well-typed wrapper that adds validation, error translation, and dependency checks.

### Status Check

`GET /telegram/status` answers the simplest question: is Telegram ready to use? It loads the current settings and returns `configured=True` only when both `telegram_bot_token` and `allowed_user_id` are set. The conjunction matters — a bot token without a linked user ID means the bot is installed but nobody has completed the pairing handshake. The endpoint exposes `user_id` so the dashboard can display which Telegram account is linked.

### Pairing Initiation

`POST /telegram/setup` is the most complex endpoint in this file. It performs three defensive checks before doing any real work:

1. **Library presence check** — It attempts to import `telegram` and `telegram.ext` from `python-telegram-bot`. If the import fails, it raises 501 Not Implemented with an actionable install message. This prevents a confusing `ImportError` stack trace from surfacing to the client and signals the correct remediation step.

2. **Dashboard state check** — It imports `_telegram_pairing_state` from `pocketpaw.dashboard`. If the dashboard module is not running (e.g., PocketPaw was started in headless API mode), it raises 503 Service Unavailable. The pairing flow depends on the dashboard's event loop and state dict, so it cannot operate independently.

3. **Token format validation** — It calls `validate_api_key("telegram_bot_token", bot_token)` before launching the temporary bot. Telegram bot tokens follow a specific format (`{id}:{hash}`), and an invalid token would fail only after the temporary bot attempts its first API call — a slow failure. Validating upfront gives the user immediate feedback.

After these guards pass, the router launches the temporary bot, generates a QR code URL and deep link, and returns them in a `TelegramSetupResponse`.

### Pairing Status Polling

`GET /telegram/pairing-status` is designed for short-interval polling. It reads from the shared `_telegram_pairing_state` dict populated by the temporary bot's event handlers. When the bot receives a `/start` command from the expected user, it sets `paired=True` in the state dict and records the user's Telegram ID. The endpoint serializes this state into a `TelegramPairingStatusResponse`.

### Shared State Coupling

The shared `_telegram_pairing_state` dict is imported from `dashboard.py` at request time (inside the function body, not at module load). This lazy import pattern prevents circular import failures when the router module is loaded before the dashboard is fully initialized.

### Known Gaps

The pairing state is stored in a module-level dict with no expiration logic visible in the router. If a pairing attempt is abandoned mid-flow (the user never clicks the deep link), the temporary bot remains running until the process restarts. A cleanup mechanism — either a background task that kills the temporary bot after a TTL or an explicit cancel endpoint — is not present. There is also no rate limiting on `POST /telegram/setup`, meaning repeated calls would spawn multiple temporary bots against the same token.