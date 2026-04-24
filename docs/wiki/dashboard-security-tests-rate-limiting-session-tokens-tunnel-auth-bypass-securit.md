---
{
  "title": "Dashboard Security Tests: Rate Limiting, Session Tokens, Tunnel Auth Bypass, Security Headers, and WebSocket Auth",
  "summary": "This comprehensive test module validates PocketPaw's dashboard security hardening across six dimensions: token-bucket rate limiting, HMAC-signed session tokens with TTL, the localhost tunnel auth bypass fix, security headers, CORS rejection, and WebSocket authentication before the upgrade handshake completes.",
  "concepts": [
    "RateLimiter",
    "token bucket",
    "session tokens",
    "HMAC",
    "TTL",
    "_is_genuine_localhost",
    "tunnel auth bypass",
    "security headers",
    "HSTS",
    "WebSocket auth",
    "CORS",
    "rate limiting",
    "Sec-WebSocket-Protocol",
    "CSP"
  ],
  "categories": [
    "security",
    "testing",
    "dashboard",
    "authentication",
    "test"
  ],
  "source_docs": [
    "48e376df5188e862"
  ],
  "backlinks": null,
  "word_count": 637,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's dashboard exposes agent management endpoints that must be protected even when the agent is accessible over a public tunnel (e.g., ngrok, Cloudflare Tunnel). This file tests the layered defense that prevents unauthorized access, rate-limit bypass, session forgery, and WebSocket hijacking.

## Rate Limiter (`TestRateLimiter`)

`RateLimiter` implements a token-bucket algorithm with per-IP isolation. Key behaviors tested:

- **Within capacity**: up to `capacity` requests are allowed before any token refill.
- **Over capacity**: the next request after the bucket empties is rejected (`allow()` returns `False`).
- **Refill**: the test manipulates `last_refill` directly to simulate elapsed time, confirming that tokens accumulate at the configured rate.
- **Per-IP isolation**: depleting `ip1`'s bucket doesn't affect `ip2`'s bucket — a critical property that prevents one abusive client from blocking all others.
- **Stale bucket cleanup**: `cleanup(max_age=3600)` removes buckets last accessed more than an hour ago, preventing unbounded memory growth in long-running deployments.

## Session Tokens (`TestSessionTokens`)

Session tokens are HMAC-SHA256 signed strings with embedded timestamps. Tests cover:

- **Round-trip**: `create_session_token` + `verify_session_token` with the same master key passes.
- **Expired token**: A token with a past timestamp but a valid HMAC is rejected. The test constructs a syntactically valid expired token using `_sign` directly, confirming the TTL check happens before (or independently of) the signature check.
- **Tampered token**: Modifying the timestamp or signature bytes causes rejection.
- **Wrong master key**: A token signed with master key A is rejected when verified against master key B.
- **Invalid format**: Tokens without the expected `:` separator are rejected.
- **Master regeneration**: Generating a new master key invalidates all previously issued session tokens, providing an emergency revocation mechanism.

## Localhost Tunnel Auth Bypass (`TestIsGenuineLocalhost`)

When a tunnel is active (ngrok, etc.), requests arrive at the PocketPaw server with the tunnel's proxy headers (`X-Forwarded-For`, `CF-Connecting-IP`). A naive localhost bypass that checks only the `Host` header would allow tunnel traffic to skip authentication.

`_is_genuine_localhost` detects the bypass attempt by checking for proxy headers alongside a localhost host. Tests cover:
- Genuine localhost with no tunnel active → bypass allowed.
- Tunneled request with `X-Forwarded-For` → bypass blocked.
- Tunneled request with `CF-Connecting-IP` → bypass blocked.
- Genuine localhost with active tunnel but no proxy headers → bypass still allowed (direct local access).
- Non-localhost host → always rejected regardless of tunnel state.
- IPv6 loopback (`::1`) → treated as genuine localhost.
- Bypass disabled via settings → all requests require authentication.

## Security Headers and Frontend Safety (`TestSecurityHeaders`, `TestFrontendSvgSafety`)

HTTP security headers (CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy) are asserted present. HSTS is only set when the connection is HTTPS, preventing HSTS preload on HTTP deployments.

The frontend SVG safety tests confirm that the memory graph uses a sanitized SVG insertion pattern and that memory prune/delete operations require explicit user confirmation — preventing accidental data loss from UI misclicks.

## Login Rate Limiting (`TestLoginRateLimit`)

Auth endpoints are rate-limited even though they are exempt from token authentication (since you need to call them unauthenticated to get a token). Tests mock the rate limiter's `allow()` method to return `False` and assert the endpoint returns 429. Audit logging on rate-limit blocks is also verified. Static asset endpoints are explicitly not rate-limited.

## WebSocket Authentication (`TestWebSocketMiddlewareAuth`)

WebSocket authentication must occur before the upgrade handshake completes, so that unauthorized clients never establish a persistent connection. The middleware validates tokens from three sources: query parameter, cookie, and `Authorization: Bearer` header. Tests also cover the `Sec-WebSocket-Protocol` subprotocol token delivery pattern used by browser WebSocket clients that cannot set custom headers.

## Known Gaps

No TODO or FIXME markers are present. CORS rejection of non-matching origins is listed in the module docstring as a covered area, but the test class for CORS is not present in the AST output, suggesting it may be in a separate file or was planned but not yet written.