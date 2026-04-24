---
{
  "title": "Remote Access Auth Tests: Token Generation, Middleware, and QR Endpoint Security",
  "summary": "PocketPaw's dashboard uses a file-backed token for remote access authentication. These tests validate token generation and persistence, the auth middleware's enforcement of Bearer token and query parameter authentication, and a security fix (issue #854) that closed an unauthenticated `/api/qr` endpoint.",
  "concepts": [
    "remote access",
    "token authentication",
    "Bearer token",
    "query parameter auth",
    "QR endpoint",
    "auth middleware",
    "FastAPI TestClient",
    "issue #854",
    "config directory isolation",
    "UUID token"
  ],
  "categories": [
    "testing",
    "security",
    "dashboard authentication",
    "test"
  ],
  "source_docs": [
    "07a9d0d0a652aa86"
  ],
  "backlinks": null,
  "word_count": 490,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's dashboard is served locally but accessible from mobile devices — for example, via the QR code endpoint. Without authentication, any device on the same network could access the dashboard. The token system uses a UUID stored in the config directory as a simple but effective shared secret.

## Token Generation and Persistence

`test_token_generation` validates the token lifecycle:

```python
def test_token_generation(mock_config, tmp_path):
    token_path = tmp_path / "access_token"
    token_path.unlink(missing_ok=True)
    token = get_access_token()
    assert token is not None
    assert len(token) > 20
    assert token_path.exists()
    assert token_path.read_text(encoding="utf-8") == token
```

This test verifies four things: the token is non-null, long enough to be a real UUID, written to disk, and that the file contents match the returned value. The `missing_ok=True` ensures the test starts clean regardless of previous runs.

The `mock_config` fixture patches `get_config_dir` to return `tmp_path`, preventing tests from reading or writing the real config directory. This is critical for test isolation — without it, tests would interfere with each other and with any running PocketPaw instance.

## Auth Middleware: Deny Without Token

`test_auth_middleware_deny` verifies the baseline security posture:

- Accessing `/api/identity` without any token returns `401 Unauthorized`.
- The response body is `{"detail": "Unauthorized"}` — not an HTML error page.

The JSON error format is important because frontend clients and other agents parse this response programmatically.

## Auth Middleware: Allow with Bearer Header

`test_auth_middleware_allow_header` verifies the primary auth path:

```python
headers = {"Authorization": f"Bearer {token}"}
response = client.get("/api/identity", headers=headers)
assert response.status_code != 401
```

The test acknowledges that downstream failures (missing settings file) may return 500 but verifies the request passes the auth layer. The comment `"If internal error, 500. Since we fixed the import, ideally 200"` documents a known fragility in the test environment setup.

## Auth Middleware: Allow with Query Parameter

`test_auth_middleware_allow_query_param` validates the query parameter auth path (`?token=...`), used by QR code scans and mobile browsers where setting custom headers is difficult.

## QR Endpoint Security Fix (Issue #854)

`test_qr_endpoint_requires_auth` documents a specific security vulnerability that was fixed:

> The QR endpoint was previously exempt from auth, allowing any network-reachable client to obtain a valid session token.

```python
def test_qr_endpoint_requires_auth():
    client = TestClient(app)
    response = client.get("/api/qr")
    assert response.status_code == 401
```

The QR code contains the session token — it is the bootstrap mechanism for mobile access. An unauthenticated QR endpoint would allow anyone on the network to obtain the dashboard token by making a single HTTP request, completely bypassing the intended security model.

`test_qr_endpoint_allowed_with_auth` confirms the endpoint still works for authenticated requests and returns a PNG image.

## Known Gaps

- No test for token rotation — what happens when the token file is deleted after the server starts.
- No test for expired tokens — the current implementation does not appear to expire tokens.
- No test for concurrent requests with the same token (thread safety of `get_access_token()`).
- The 500 vs 200 ambiguity in `test_auth_middleware_allow_header` indicates incomplete test setup that should be resolved.