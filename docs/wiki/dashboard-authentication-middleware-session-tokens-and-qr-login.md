---
{
  "title": "Dashboard Authentication: Middleware, Session Tokens, and QR Login",
  "summary": "dashboard_auth.py centralizes all authentication logic for the PocketPaw dashboard — localhost detection, HMAC token verification, session token exchange, cookie-based login/logout, and QR code generation. It was extracted from dashboard.py to isolate auth concerns and make each piece independently testable.",
  "concepts": [
    "authentication",
    "ASGI middleware",
    "session tokens",
    "HMAC",
    "localhost detection",
    "QR code login",
    "rate limiting",
    "upload grants",
    "cookie auth",
    "WebSocket auth"
  ],
  "categories": [
    "Dashboard",
    "Security"
  ],
  "source_docs": [
    "22a7fae8f3854d27"
  ],
  "backlinks": null,
  "word_count": 486,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/dashboard_auth.py` is PocketPaw's authentication layer, extracted from the monolithic `dashboard.py` to give auth concerns a single, auditable home. It covers HTTP middleware, standalone token verification, session token lifecycle, QR-based mobile login, and signed upload grant verification.

## Genuine Localhost Detection

`_is_genuine_localhost()` checks whether an HTTP or WebSocket request truly originates from the local machine, not from a tunneled proxy (e.g., ngrok, Cloudflare Tunnel). This matters because PocketPaw's auto-login bypass applies only to genuine local connections — if a proxy adds an `X-Forwarded-For` or similar header, the request should be treated as remote.

The function checks `_PROXY_HEADERS` (a set of forwarding headers) and `_LOCALHOST_ADDRS` (127.0.0.1, ::1, localhost). If any proxy header is present or the client IP isn't in the localhost set, it returns `False`.

## AuthMiddleware (ASGI)

`AuthMiddleware` is implemented as a raw ASGI middleware class rather than a FastAPI dependency. This is intentional: FastAPI dependencies run after routing, but auth needs to intercept requests before any route handler executes — including WebSocket upgrades. The raw ASGI approach also handles both HTTP and WebSocket scopes in a single class.

The middleware:
1. Lets `OPTIONS` requests through (CORS preflight must not be blocked).
2. Checks `_ws_scope_auth_ok()` for WebSocket upgrades — reads the `cookie` and `token` query param from the ASGI scope headers.
3. For HTTP, checks the session cookie, the `Authorization` header, or the `?token=` query param.
4. Auto-approves genuine localhost requests (dev mode bypass).

## Session Token Exchange

`exchange_session_token()` trades a master access token for a time-limited session token. The session token format is `"{expires_unix}:{hmac}"` where the HMAC is computed over `"{token}:{expires}"` using the master token as the key. Clients store this in a cookie and present it on subsequent requests.

Session tokens expire (configurable, default short-lived) so a compromised cookie has a limited window. The master token itself never touches the browser cookie — only the derived session token does.

## Upload Grant Verification

`_verify_upload_grant()` handles a special bypass for signed file download links. The route pattern `^/api/v1/uploads/(?P<id>[A-Za-z0-9_-]+)$` is explicitly scoped so the grant can only be used to reach upload endpoints, not arbitrary dashboard routes. This prevents a signed link from being replayed against `/api/settings` or similar sensitive paths.

## Rate Limiting Integration

`auth_router` endpoints are protected by `api_limiter` and `auth_limiter` from `pocketpaw.security.rate_limiter`. Auth endpoints (login, token exchange) use the stricter `auth_limiter` to slow brute-force attempts. General API endpoints use the more permissive `api_limiter`.

## QR Code Login

The `auth_router` includes a QR code generation endpoint that encodes the dashboard URL and access token into a QR image (PNG, base64). This is used for mobile access — the user scans the QR with their phone to authenticate without typing the token manually.

## Known Gaps

- The session token HMAC uses the master token as the key. If the master token is short or predictable (e.g., user chose a simple value), session tokens are weaker. The system doesn't enforce minimum token entropy.