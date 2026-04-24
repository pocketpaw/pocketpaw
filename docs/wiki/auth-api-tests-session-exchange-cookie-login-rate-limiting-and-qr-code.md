---
{
  "title": "Auth API Tests: Session Exchange, Cookie Login, Rate Limiting, and QR Code",
  "summary": "This test module comprehensively covers PocketPaw's `/api/v1/auth` router, verifying the master-token-to-session exchange, cookie-based login with correct Secure flag handling under reverse proxies, logout, token regeneration, QR code generation, and IP-based rate limiting on all three auth endpoints.",
  "concepts": [
    "session token",
    "master token",
    "cookie login",
    "Secure flag",
    "X-Forwarded-Proto",
    "rate limiting",
    "auth_limiter",
    "QR code",
    "token regeneration",
    "logout",
    "TestClient",
    "FastAPI router testing",
    "429 rate limit"
  ],
  "categories": [
    "authentication",
    "security",
    "API",
    "testing",
    "test"
  ],
  "source_docs": [
    "a91d4483c725a436"
  ],
  "backlinks": null,
  "word_count": 525,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's auth layer uses a two-token model: a long-lived master token (stored in config) and short-lived session tokens issued against it. This test file exercises the full surface of that system via FastAPI's `TestClient`, isolating external dependencies through `unittest.mock.patch`.

## Fixture Design

A module-level `autouse` fixture `_allow_auth_rate_limiter` patches `auth_limiter.allow` to return `True` for all tests by default. This prevents tests unrelated to rate limiting from failing if the real limiter rejects requests during CI. Individual tests in `TestAuthRateLimiting` override this fixture with their own `@patch` to exercise the 429 path — the docstring explicitly documents this layering to prevent confusion.

## Session Exchange (`POST /auth/session`)

`TestSessionExchange` verifies the Bearer-token-to-session flow:

- A valid `Authorization: Bearer <master_token>` header yields a 200 with `session_token` and `expires_in_hours`.
- A wrong token yields 401. No session is created for any non-matching token, preventing brute-force token discovery through timing or error-message differences.
- A missing header also yields 401, distinguishing the unauthenticated case from an invalid credential.

## Cookie Login (`POST /auth/login`)

`TestCookieLogin` covers the browser-oriented login flow that sets a `pocketpaw_session` cookie:

- **Secure flag logic**: The `Secure` flag on the cookie is set only when the request arrives over HTTPS. PocketPaw runs locally over HTTP by default, so a plain local login should not set `Secure` (which would prevent the browser from sending the cookie). Tests for `X-Forwarded-Proto: https` and `X-Forwarded-Proto: HTTPS, http` (multi-hop proxy chains) confirm the server correctly promotes the cookie to `Secure` when behind a TLS-terminating proxy — a common production deployment pattern.
- **Invalid JSON**: Sending non-JSON content with `Content-Type: application/json` returns 400, not a 500 internal error. This protects against malformed client requests causing unhandled exceptions.

```python
def test_login_sets_secure_cookie_for_multihop_forwarded_proto(
    self, mock_create, mock_load, mock_get, client
):
    # X-Forwarded-Proto: HTTPS, http  (outer proxy first, inner HTTP)
    resp = client.post(..., headers={"X-Forwarded-Proto": "HTTPS, http"})
    assert "Secure" in resp.headers["set-cookie"]
```

## Logout and Token Regeneration

Logout simply clears the session cookie and returns `{"ok": true}` — it is stateless from the server's perspective. Token regeneration (`POST /token/regenerate`) delegates to `config.regenerate_token` and returns the new token; the test verifies the delegate was called exactly once, preventing double-regeneration bugs.

## QR Code

`TestQRCode` confirms the `/qr` endpoint returns a valid PNG (content-type and minimum size check). Two variants are tested: one without an active tunnel (QR encodes the local URL) and one with an active Cloudflare tunnel URL, verifying the tunnel URL is preferred when available.

## Rate Limiting

`TestAuthRateLimiting` covers issue #628. All three auth-facing endpoints (`/auth/session`, `/auth/login`, `/qr`) must:

1. Return 429 with `{"detail": "Too many requests"}` when `auth_limiter.allow()` returns `False`.
2. Pass the client IP to `auth_limiter.allow()` so the limiter can track per-IP state.
3. Still return 200 when the limiter allows the request.

The tests patch `pocketpaw.security.rate_limiter.auth_limiter` directly (not an import alias) because the handler imports the limiter lazily inside the function body — a deliberate design choice to make the patch target stable.

## Known Gaps

No `TODO` or `FIXME` markers are present. The test suite does not cover: concurrent login attempts from the same IP, session token TTL expiry behaviour in the session-exchange response, or what happens when `create_session_token` raises an exception.