---
{
  "title": "Web Server Security Tests: Bot Token Redaction in /setup Errors",
  "summary": "This module tests that the `/setup` endpoint in PocketPaw's web server never leaks the Telegram bot token in HTTP error responses (F-07, issue #445). It verifies that when the upstream Telegram API returns an error whose message embeds the token URL, the response is redacted before reaching the client.",
  "concepts": [
    "bot token redaction",
    "F-07",
    "security hardening",
    "Telegram",
    "/setup endpoint",
    "credential leak",
    "TestClient",
    "create_app",
    "error sanitization",
    "[REDACTED]",
    "web_server"
  ],
  "categories": [
    "testing",
    "security",
    "web server",
    "credential protection",
    "test"
  ],
  "source_docs": [
    "5cf6ebc4b2081154"
  ],
  "backlinks": null,
  "word_count": 445,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_web_server_security.py` is a security-focused test module covering F-07 from the PocketPaw security checklist (tracked in issue #445). The failure scenario it prevents: when `bot.get_me()` fails during `/setup`, the python-telegram-bot library raises an exception whose message contains the full API URL—including the bot token in plaintext (e.g., `https://api.telegram.org/bot<TOKEN>/getMe`). Without redaction, this token would appear verbatim in the JSON error response returned to the browser, leaking credentials to anyone who can read the response (users, browser DevTools, proxies, logs).

## The Threat Model

Telegram bot tokens are long-lived API credentials. If exposed, an attacker can impersonate the bot, read all conversations, and send messages on its behalf. The `/setup` endpoint is particularly dangerous because it receives the token as form input and immediately tests it by calling Telegram—meaning an error during that call has the token in scope and inside the exception message.

## Test: `test_bot_token_redacted_from_error_response`

This is the primary regression guard. It:

1. Constructs a realistic fake token (`123456:AAFakeToken-TestOnly_1234567890abcdef`).
2. Simulates the exact error python-telegram-bot produces on API failure—a message embedding the token inside a Telegram URL.
3. Patches `pocketpaw.web_server.Application` so `bot.get_me()` raises that exception.
4. Calls `/setup` via Starlette's `TestClient`.
5. Asserts **the raw token does not appear anywhere** in `resp.json()["error"]`.
6. Asserts `[REDACTED]` is present instead—confirming active sanitization, not just accidental omission.

The last assertion is critical: a naive implementation might strip the token field without replacing it, making the redaction invisible and harder to audit.

## Test: `test_error_without_token_passes_through`

This is the false-positive guard. A generic error like "Network is unreachable" must pass through unchanged—no `[REDACTED]` in the output. Without this test, an overly aggressive redaction implementation could blank out all error messages, hiding legitimate diagnostic information.

## Fixture: `_fake_settings`

A minimal mock `Settings` object with all credential fields set to empty strings. This isolates `create_app` from the host environment; without it, tests would fail on machines without a `.env` file or would accidentally use real credentials.

## Design Notes

The test imports `create_app` and `TestClient` inside the test body (not at module level). This is intentional: `create_app` reads module-level state at import time, so patching must happen before the import. The `with patch(...)` context manager wraps both the patch and the import, ensuring the mock is in place before the constructor runs.

## Known Gaps

- Only Telegram token redaction is tested. If PocketPaw adds OpenAI or Anthropic API keys to `/setup` error paths, those would not be covered by this suite.
- The test does not verify that server logs (stdout/stderr) are also free of the token—only the HTTP response body.
- No test covers partial-token exposure (e.g., first N characters of the token appearing in a truncated error message).
