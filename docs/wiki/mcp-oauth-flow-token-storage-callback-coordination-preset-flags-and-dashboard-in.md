---
{
  "title": "MCP OAuth Flow: Token Storage, Callback Coordination, Preset Flags, and Dashboard Integration",
  "summary": "The MCP OAuth test suite validates PocketPaw's support for OAuth-authenticated MCP servers, covering secure token and client-info persistence, WebSocket-based callback coordination, preset flag propagation, and the unauthenticated OAuth callback dashboard endpoint. Tests also verify that token files are created with restricted permissions and that corrupted token files degrade gracefully.",
  "concepts": [
    "OAuth",
    "MCPTokenStorage",
    "OAuthToken",
    "OAuthClientInfo",
    "callback coordination",
    "state parameter",
    "Future",
    "file permissions",
    "201 status code",
    "preset flags",
    "dashboard endpoint",
    "auth exempt"
  ],
  "categories": [
    "MCP integration",
    "authentication",
    "test"
  ],
  "source_docs": [
    "bd77bcb1f28ac2fe"
  ],
  "backlinks": null,
  "word_count": 522,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Some MCP servers (e.g., Notion, Supabase) require OAuth rather than static API keys. PocketPaw implements a browser-redirect OAuth flow: the user is sent to the provider's auth page, authorizes, and the provider redirects back to a local callback endpoint. The `MCPTokenStorage`, callback coordination system, and dashboard endpoint together form this flow.

## Token Storage

`TestMCPTokenStorage` uses `MCPTokenStorage` — a per-server file-based store for `OAuthToken` and `OAuthClientInfo` objects. Key behaviors:

- **Empty store**: `get_tokens()` returns `None` (not an empty object) so callers can distinguish "never authenticated" from "token is present but expired".
- **Roundtrip**: set then get preserves `access_token`, `refresh_token`, and `token_type`.
- **Client info**: stored separately from tokens in the same file.

`test_file_permissions` verifies that the token file is created with mode `0o600` (owner read/write only). This is a security requirement — token files on a shared system must not be readable by other users.

`test_corrupted_file_returns_none` verifies that a JSON file with garbage content returns `None` rather than raising `JSONDecodeError`. This handles the case where a partial write (e.g., disk full during token refresh) corrupts the file.

## 201 Token Response Handling

`TestOAuthCompatProvider` addresses a specific interoperability issue: some OAuth servers (notably Supabase) return `HTTP 201 Created` for the token endpoint instead of the RFC-standard `200 OK`. `test_accepts_201_token_response` and `test_accepts_201_refresh_response` confirm that the OAuth provider implementation accepts both status codes, preventing authentication failures against non-standard servers.

## Callback Coordination

`TestOAuthCallbackCoordination` tests the mechanism that links the OAuth redirect back to the waiting connection attempt:

- **`set_oauth_callback_result`**: resolves a `Future` keyed by the OAuth `state` parameter. The waiting `start_server()` call blocks on this future.
- **Unknown state**: returns gracefully — a redirect with an unknown or replayed `state` is ignored.
- **Already resolved**: a second call with the same `state` is safely ignored, preventing a double-resolve panic.
- **WebSocket broadcast**: `set_ws_broadcast()` registers a function for pushing OAuth status updates to the dashboard UI in real time.

## Preset OAuth Flags

`TestPresetOAuthFlags` validates that the preset catalog correctly marks HTTP-transport presets as `oauth=True` and stdio presets as `oauth=False`, and that `preset_to_config()` propagates the flag to the resulting `MCPServerConfig`. If the flag were lost during conversion, the connection manager would not initiate the OAuth flow.

## Config Dict Serialization

`TestConfigOAuthField` verifies that `to_dict()` includes `oauth: true` only when the flag is `True` (not when `False`), keeping config files clean. `from_dict()` defaults `oauth` to `False` when the key is absent — backward compatible with configs written before OAuth support was added.

## Dashboard OAuth Callback Endpoint

`TestDashboardOAuthCallback` tests the `/api/mcp/oauth/callback` route:

- **Auth exempt**: the callback endpoint must not require a PocketPaw auth token, because it is called by the OAuth provider's redirect, not by the user's browser session.
- **Success**: `code` + `state` params trigger `set_oauth_callback_result` with the code.
- **Expired flow**: a `state` with no waiting future returns an appropriate error.
- **Missing params**: missing `code` or `state` returns HTTP 400.

## Known Gaps

- Token refresh flows (using `refresh_token` to obtain a new `access_token`) are not tested end-to-end — only the storage of refresh tokens is validated.
- The `test_file_permissions` test is skipped on Windows where Unix permissions do not apply.