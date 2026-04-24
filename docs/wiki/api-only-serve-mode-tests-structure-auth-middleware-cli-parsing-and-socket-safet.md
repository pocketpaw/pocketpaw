---
{
  "title": "API-Only Serve Mode Tests: Structure, Auth Middleware, CLI Parsing, and Socket Safety",
  "summary": "Tests for PocketPaw's `serve` command, which launches a lightweight API-only server without the dashboard UI. Covers endpoint availability, auth middleware behavior, CLI argument parsing, and a regression suite proving that sockets used for local IP detection are always closed — even when errors occur during detection.",
  "concepts": [
    "serve command",
    "API-only server",
    "headless mode",
    "auth middleware",
    "CORS preflight",
    "socket resource safety",
    "file descriptor leak",
    "local IP detection",
    "CLI argparse",
    "dashboard exclusion"
  ],
  "categories": [
    "testing",
    "API server",
    "CLI",
    "security",
    "test"
  ],
  "source_docs": [
    "fd5bb95c6b4c97fc"
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

## Overview

`tests/test_api_serve.py` covers the `pocketpaw serve` command, which starts PocketPaw as a headless API server suitable for embedding in larger systems, running in containers, or exposing to external clients without the interactive dashboard. The serve mode has different security characteristics from the dashboard mode and requires its own test coverage.

## API App Structure

`TestAPIAppStructure` creates the lightweight API app via the `api_app` fixture and verifies all expected endpoints are present:
- OpenAPI JSON (`/api/v1/openapi.json`), docs (`/api/v1/docs`), and redoc pages.
- `/api/v1/health` — liveness/readiness probe.
- `/api/v1/backends`, `/api/v1/sessions`, `/api/v1/skills` — core agent runtime endpoints.
- `/api/v1/version` — version endpoint for external orchestrators.

All tests in this class use `@patch("pocketpaw.dashboard_auth._is_genuine_localhost", return_value=True)` to bypass the localhost guard, isolating the structural tests from the auth middleware.

## No Dashboard UI

`TestNoDashboardUI` verifies the serve app does NOT serve the web dashboard:

- `test_no_root_html` confirms `GET /` does not return HTML (the serve app should return 404 or JSON, not an SPA shell).
- `test_websocket_endpoint_exists` confirms a WebSocket endpoint is present — agent runners may need to connect over WebSocket even in serve mode.

**Why this matters:** Accidentally including the dashboard in the serve app would expose the UI to external networks when serve is used for API-only deployments, increasing the attack surface.

## Auth Middleware

`TestAuthMiddleware` tests the API key authentication middleware that protects the serve app:

- `test_unauthenticated_request_blocked` — requests without a valid token return 401.
- `test_options_preflight_passes_without_auth` — CORS preflight OPTIONS requests must pass without authentication (browsers send these before the actual request).
- `test_cors_headers_on_allowed_origin` — allowed origins receive CORS headers.
- `test_docs_exempt_from_auth` and `test_openapi_json_exempt_from_auth` — documentation endpoints must be accessible without auth so that developers can discover the API before obtaining a key.

**Failure scenario prevented:** If docs endpoints require auth, developers cannot access the API reference to learn how to authenticate in the first place.

## CLI Argument Parsing

`TestServeCommand` verifies the `serve` subcommand is recognized by PocketPaw's argparse configuration and that `--host` and `--port` arguments are accepted. These tests run at the CLI layer, not the HTTP layer.

## Socket Resource Safety

`TestSocketResourceSafety` is a regression test suite for a specific bug class: PocketPaw uses a UDP socket to detect the machine's local IP address (for display purposes). If socket operations raise an exception (e.g., `ENETUNREACH` in a container with no network), the socket must still be closed to avoid leaking file descriptors.

Six tests cover the matrix:
- Socket closed on successful connect and `getsockname`.
- Socket closed when `connect()` raises.
- Socket closed when `getsockname()` raises.
- Fallback IP (`127.0.0.1`) is used when detection fails.
- Same coverage for the dashboard app's socket.

The `_make_mock_socket()` helper constructs `MagicMock` socket objects with configurable failure points, avoiding the need for real network calls.

## Known Gaps

No TODO or FIXME markers. The auth middleware tests do not cover rate limiting behavior in serve mode (rate limiter is tested separately in `test_api_rate_limits.py`).