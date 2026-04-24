---
{
  "title": "Dashboard Auth Cookie Tests: Secure Flag Behavior Under HTTP and HTTPS Proxies",
  "summary": "This test file validates that the dashboard's session cookie is set without the `Secure` flag in plain HTTP environments (to avoid breaking self-hosted HTTP deployments) and with the `Secure` flag when the request arrives via an HTTPS reverse proxy detected by the `X-Forwarded-Proto: https` header.",
  "concepts": [
    "session cookie",
    "Secure flag",
    "X-Forwarded-Proto",
    "HTTPS proxy",
    "dashboard auth",
    "auth_router",
    "create_session_token",
    "TestClient",
    "FastAPI",
    "SameSite",
    "HttpOnly",
    "reverse proxy"
  ],
  "categories": [
    "security",
    "testing",
    "dashboard",
    "authentication",
    "test"
  ],
  "source_docs": [
    "103f8a471327cc45"
  ],
  "backlinks": null,
  "word_count": 415,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `Secure` cookie attribute tells browsers to send the cookie only over HTTPS connections. Setting it unconditionally would break the common PocketPaw self-hosted deployment pattern where the agent runs on `http://localhost` or `http://192.168.x.x` behind a local network without TLS. But omitting `Secure` on production deployments served over HTTPS would allow the session cookie to leak over plain HTTP requests.

The correct behavior is to infer the connection security from the `X-Forwarded-Proto` header, which is set by nginx, Caddy, Cloudflare, and other reverse proxies when they terminate TLS. This test file validates exactly that conditional logic.

## Test Setup

A minimal `FastAPI` app is created with only `auth_router` mounted. The `TestClient` from `fastapi.testclient` is used so tests run synchronously without a live server. Three dependencies are patched for each test:
- `get_access_token`: returns the `MASTER_TOKEN` constant, bypassing the real token storage lookup.
- `Settings.load`: returns a `MagicMock` with `session_token_ttl_hours=24`.
- `create_session_token`: returns a predictable `"sess:xyz"` token string.

This isolation ensures the cookie behavior is tested independently of credential storage and token generation logic.

## Test 1: No Secure Flag on Plain HTTP

`test_login_sets_cookie_without_secure_by_default` POSTs to `/api/auth/login` without any `X-Forwarded-Proto` header. Assertions:
- Response status is 200.
- `resp.json()["ok"]` is `True`.
- `"pocketpaw_session"` is present in `resp.cookies`.
- `"Secure"` is **not** in the `set-cookie` header.

This confirms that a plain HTTP deployment — the typical self-hosted case — receives a functional cookie that browsers will actually send.

## Test 2: Secure Flag on Forwarded HTTPS

`test_login_sets_secure_cookie_for_forwarded_https` adds `X-Forwarded-Proto: https` to the request headers. The single changed assertion is that `"Secure"` **is** present in the `set-cookie` header. Everything else is identical.

This confirms the header detection is working. Without this behavior, a production deployment behind nginx (which sets `X-Forwarded-Proto`) would issue cookies without the `Secure` flag, allowing them to be sent over downgraded HTTP connections in mixed-content scenarios.

## Why These Two Cases Are Both Necessary

If the code always set `Secure`, local HTTP deployments would break: browsers would refuse to send the cookie to `http://localhost`, logging out the operator on every page load. If the code never set `Secure`, HTTPS deployments would have an unnecessary attack surface. The dual test ensures neither regression is introduced.

## Known Gaps

The `HttpOnly` and `SameSite` cookie attributes are not asserted in these tests. A `SameSite=Strict` or `SameSite=Lax` attribute would provide CSRF protection; its absence from the test means it could be accidentally removed without a test failure. The `Secure` attribute is the only cookie security property verified here.