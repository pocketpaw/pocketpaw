---
{
  "title": "Auth Router — Session Exchange, Cookie Login, QR Code, and Token Regeneration",
  "summary": "The auth router handles all first-party authentication flows: exchanging a master access token for a time-limited session token, setting and clearing HTTP-only session cookies, generating QR login codes for mobile pairing, and regenerating the master token when credentials need to be rotated. It uses constant-time comparison to close a timing oracle vulnerability in master-token validation.",
  "concepts": [
    "session token",
    "master access token",
    "hmac.compare_digest",
    "timing oracle",
    "HTTP-only cookie",
    "rate limiting",
    "QR code login",
    "token regeneration",
    "Bearer auth",
    "auth flow",
    "cookie security"
  ],
  "categories": [
    "API",
    "Security",
    "Authentication"
  ],
  "source_docs": [
    "b6e0389dbb5ad543"
  ],
  "backlinks": null,
  "word_count": 425,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`auth.py` is the authentication gateway for the PocketPaw dashboard and API clients. It supports multiple login modalities — Bearer token exchange, cookie-based browser sessions, and QR code pairing — all anchored to a single master access token configured at install time.

## Session Token Exchange

The primary auth flow works in two steps: a client presents the master access token via `Authorization: Bearer <token>`, and receives back a short-lived session token. This separation exists so that the master token (which never expires automatically) doesn't need to travel in every subsequent request — the session token provides a time-bounded credential that expires on its own.

Rate limiting is applied at the exchange endpoint using `auth_limiter.allow(client_ip)`. Without this guard, an attacker with network access to the API could brute-force the master token by repeated POST attempts. A 429 response is returned when the rate limit is exceeded.

## Timing Oracle Fix (`hmac.compare_digest`)

A notable security hardening visible in the changelog:

> Updated: 2026-04-09 — use hmac.compare_digest for master-token comparisons to close the timing-oracle gap left open by PR #875

Standard Python `==` comparison of strings short-circuits on the first mismatched byte. An attacker can measure response time across thousands of requests to statistically infer the correct token character-by-character. `hmac.compare_digest` runs in constant time regardless of where the mismatch occurs, eliminating this side channel. PR #875 hardened `session_tokens.py` but missed the raw comparison sites in this file; the 2026-04-09 update closed the gap.

## Cookie Login and Secure Flag

`cookie_login` validates the master token and sets an HTTP-only session cookie. The `is_request_secure(request)` helper from `pocketpaw.http_utils` determines whether to apply the `Secure` flag — preventing the cookie from being sent over plain HTTP in production while not breaking local development over `http://localhost`.

`cookie_logout` simply clears the cookie, making the browser-based logout path stateless.

## QR Code Generation

`get_qr_code` generates a QR code containing the local server's URL and a temporary auth token. This enables mobile apps or companion tools to pair with a running PocketPaw instance without the user manually copying tokens. The response is a `StreamingResponse` carrying a PNG image.

## Token Regeneration

`regenerate_access_token` generates a new master access token and invalidates all existing session tokens derived from the old one. This is the nuclear option for credential rotation — use it when the master token is suspected to be compromised.

## Known Gaps

No explicit TODOs in the source. The QR code token expiry window is not documented here; if the QR token doesn't expire promptly, a shared screen or screenshot could allow unauthorized pairing.