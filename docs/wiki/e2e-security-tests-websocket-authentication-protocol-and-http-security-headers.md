---
{
  "title": "E2E Security Tests: WebSocket Authentication Protocol and HTTP Security Headers",
  "summary": "This Playwright-based suite verifies PocketPaw's WebSocket authentication model — that connections succeed from localhost without explicit auth, that the frontend sends an authenticate action as the first WebSocket message, that tokens never appear in WebSocket URLs, and that API endpoints correctly reject unauthenticated requests while static assets remain accessible.",
  "concepts": [
    "WebSocket authentication",
    "first-message auth",
    "token-in-URL prevention",
    "localhost trust",
    "Playwright network interception",
    "security headers",
    "HTTPS upgrade",
    "API authentication",
    "static asset access",
    "e2e security"
  ],
  "categories": [
    "testing",
    "security",
    "WebSocket",
    "end-to-end",
    "test"
  ],
  "source_docs": [
    "b32725240d57d22a"
  ],
  "backlinks": null,
  "word_count": 418,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw switched from query-parameter WebSocket authentication (where the token appeared in the URL) to first-message authentication (where the token is sent as the first WebSocket message payload). This test file pins that security boundary from the browser's perspective, using real Playwright browser interactions rather than simulated requests.

## Why Playwright for Security Tests?

WebSocket authentication is implemented in the browser's JavaScript. A unit test can verify the server rejects a connection, but only a browser test can verify that the client JavaScript actually sends the auth message and that the frontend JavaScript never puts the token in the URL. Playwright intercepts network traffic and reads console logs, making it possible to assert on behaviors that are invisible to the server.

## Localhost Trust Model

```python
def test_websocket_connects_on_localhost(self, page, dashboard_url):
    page.goto(dashboard_url)
    page.wait_for_timeout(2000)
    chat_input = page.locator("textarea, input[type='text']").first
    expect(chat_input).to_be_visible(timeout=5000)
```

PocketPaw trusts connections from `localhost` without requiring explicit authentication. This allows local developer usage and CLI integrations to work without token management. The test verifies this trust is actually applied — if the server incorrectly required auth even for localhost, the chat UI would never become functional.

## Token-in-URL Prevention

```python
def test_no_token_in_websocket_url(self, page, dashboard_url):
    # Assert that WebSocket URL does not contain 'token='
```

WebSocket URLs appear in browser history, server access logs, and network monitoring tools. A token in the URL is a credential leak waiting to happen. This test uses Playwright's network event interception to inspect every WebSocket connection URL made during page load and asserts none contains `token=`.

## First-Message Auth Protocol

`test_websocket_sends_auth_first_message` collects console logs emitted by the frontend JavaScript and verifies that an `authenticate` action was logged, indicating the client sent the auth message correctly. This is the positive counterpart to `test_no_token_in_websocket_url` — confirming the token was sent somewhere, just not in the URL.

## API Authentication Requirements

`TestSecurityHeaders` verifies that:
- `GET /api/...` endpoints return 401 or 403 for unauthenticated requests.
- The QR code endpoint (which generates remote access credentials) requires authentication.
- Static files (`/static/...`) are accessible without authentication (needed for CSS, JavaScript, and fonts to load before the user logs in).
- The index page is accessible without authentication (required for the login flow to work).

## Known Gaps

There is no test for the legacy query-parameter auth path — the code comment mentions it should still work, but there is no test asserting it does. There is also no test for `wss://` (TLS WebSocket) upgrade behavior, which is referenced in the comment at the top of the file.