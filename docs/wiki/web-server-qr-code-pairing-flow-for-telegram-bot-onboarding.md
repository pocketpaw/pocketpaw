---
{
  "title": "Web Server: QR Code Pairing Flow for Telegram Bot Onboarding",
  "summary": "The web server module drives PocketPaw's QR code–based Telegram pairing flow: it generates a signed deep link, serves a minimal HTML page with a scannable QR code, and simultaneously listens for the bot's `/start \u003csecret\u003e` confirmation over the Telegram API. Dynamic port allocation ensures the local server starts cleanly even when the default port is busy.",
  "concepts": [
    "QR code",
    "Telegram pairing",
    "deep link",
    "run_pairing_server",
    "find_available_port",
    "generate_qr_svg",
    "secrets.token_urlsafe",
    "FastAPI",
    "uvicorn",
    "pairing flow"
  ],
  "categories": [
    "onboarding",
    "telegram",
    "web server"
  ],
  "source_docs": [
    "0cedc342cdd02dba"
  ],
  "backlinks": null,
  "word_count": 500,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's Telegram integration requires the user to pair their account once. Rather than asking users to copy-paste a bot token or navigate a developer console, the pairing flow uses a QR code. The user scans it, Telegram opens with a pre-filled `/start` deep link, the bot receives the secret, and pairing completes without manual input.

## Deep Link + Secret Pattern

`run_pairing_server` generates a cryptographically random secret using `secrets.token_urlsafe`. This secret is embedded into a Telegram deep link: `https://t.me/{bot_username}?start={secret}`. The QR code encodes the deep link. When the user scans and taps "Open", Telegram sends `/start {secret}` to the bot. The server's `_handle_pairing_start` handler receives the message, verifies the secret matches, and signals pairing success.

Using a random secret rather than a static code prevents replay attacks: an attacker who captures a QR code in a screenshot cannot use it after the pairing session completes or times out.

## Automatic Port Allocation

`find_available_port(start_port, max_attempts)` iterates from the start port upward, attempting to bind a test socket at each port. The first free port is returned. This solves a common developer experience problem: the default pairing port may already be in use by another service, causing a confusing "address already in use" crash. By trying incrementally, the server finds a working port without user intervention.

## QR Code Generation

`generate_qr_svg` uses the `qrcode` library to produce an SVG string. SVG is chosen over PNG because it is resolution-independent (no blurry codes on high-DPI displays), embeds cleanly into an HTML `<img src="data:image/svg+xml,…">` without a file write, and contains no binary data that needs base64 encoding in the HTML payload.

## FastAPI App Architecture

`create_app(settings)` builds a minimal FastAPI application with two routes:

- `GET /` — serves the HTML page with the embedded QR code and polling instructions.
- `POST /confirm` — receives the form submission when pairing completes (fallback for environments where the Telegram bot webhook is unavailable).

The app is intentionally minimal: no authentication, no database, no persistent state. It exists only for the duration of the pairing handshake.

## Dependency Guards

Both `fastapi`/`uvicorn`/`qrcode` and `telegram`/`telegram.ext` are imported inside a `try/except ImportError` block that re-raises with an actionable install hint (`pip install 'pocketpaw[dashboard]'`). This prevents the module from crashing on import in environments where the web dashboard dependencies are not installed—only the pairing flow codepath will fail, and only when actually called.

```python
port = await run_pairing_server(settings)
# Returns the port number once pairing completes
```

## Known Gaps

- The pairing server has no timeout: if the user never scans the QR code, the server runs indefinitely. A maximum wait duration (e.g., 10 minutes) with a clean shutdown would improve the user experience for abandoned sessions.
- The HTML page served by `GET /` is hardcoded in the module; there is no template system, so customising the pairing page requires editing source code.
- Port scanning iterates sequentially; under high concurrency a race condition exists between checking a port's availability and binding it for the actual server.
