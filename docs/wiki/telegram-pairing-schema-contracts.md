---
{
  "title": "Telegram Pairing Schema Contracts",
  "summary": "Defines the Pydantic request and response models used by the Telegram channel pairing flow. These schemas enforce a clean contract between the REST API layer and the bot token validation and QR code generation logic.",
  "concepts": [
    "Telegram pairing",
    "Pydantic BaseModel",
    "bot token",
    "QR code",
    "deep link",
    "pairing status",
    "channel setup",
    "REST schemas",
    "TelegramStatusResponse",
    "TelegramSetupRequest"
  ],
  "categories": [
    "api-schemas",
    "telegram",
    "channel-integration",
    "authentication"
  ],
  "source_docs": [
    "f8e4b6b8f14de90e"
  ],
  "backlinks": null,
  "word_count": 484,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Telegram Pairing Schema Contracts

The `telegram.py` schemas module sits at the boundary between the REST API and the Telegram integration layer. Its four models encode the lifecycle of a Telegram pairing session: checking current status, submitting a bot token, receiving a scannable QR code, and polling for completion.

### Why These Models Exist

Telegram pairing involves an asynchronous multi-step flow. A user supplies a bot token, PocketPaw launches a temporary bot, generates a deep link, and the user must tap or scan it before the pairing is confirmed. Without clear schema contracts at each step, the frontend would have no reliable way to distinguish a "not yet configured" state from a "configured but user not yet linked" state or a "pairing in progress" state.

The four models map directly to those states:

- **`TelegramStatusResponse`** — answers "is Telegram currently configured?" with a boolean plus the linked numeric Telegram `user_id` if one exists. The `user_id` field is nullable because a bot token may have been stored without a user ever completing the deep-link flow.
- **`TelegramSetupRequest`** — accepts the user-supplied bot token. The `min_length=1` constraint on `bot_token` prevents accidental empty-string submissions that would silently fail downstream when the token format validator runs.
- **`TelegramSetupResponse`** — carries the QR code URL, the Telegram deep link, and an optional `error` string. Returning the error inline (rather than raising an HTTP exception) allows the client to render a degraded state — for example, displaying the deep link as a fallback when QR rendering fails.
- **`TelegramPairingStatusResponse`** — the polling model. Clients hit `/telegram/pairing-status` on a short interval while waiting for the user to click the deep link. The `paired` boolean flips to `True` once the temporary bot receives the `/start` command from the expected user.

### Defensive Choices

All boolean fields default to `False` and all optional fields default to `None` or empty string. This means a partial response (e.g., a backend that cannot reach the Telegram API) always deserializes cleanly on the client side rather than raising a validation error. The client can check `configured=False` as a safe fallback without null-guarding every field.

The `bot_token` validation in `TelegramSetupRequest` is intentionally minimal — `min_length=1` only. Full token format validation (`^[0-9]+:[A-Za-z0-9_-]{35}$`) happens in the router layer via `validate_api_key`, which means the schema stays transport-agnostic and the validation logic stays centralized.

### Integration Pattern

```python
# Typical pairing flow
request = TelegramSetupRequest(bot_token="123456:ABC-DEF...")
# POST /telegram/setup → TelegramSetupResponse
response = TelegramSetupResponse(qr_url="...", deep_link="tg://...")
# Poll GET /telegram/pairing-status → TelegramPairingStatusResponse
status = TelegramPairingStatusResponse(paired=True, user_id=987654321)
```

### Known Gaps

There is no `expires_at` field on `TelegramSetupResponse`. The temporary bot launched during setup has an implicit TTL managed by the pairing state dict in `dashboard.py`, but the client has no way to surface a "pairing expired, please restart" message without probing for a non-`paired` response after a fixed timeout. Adding an `expires_at` ISO timestamp would allow the UI to show a countdown and auto-retry.