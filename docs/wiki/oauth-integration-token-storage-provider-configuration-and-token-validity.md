---
{
  "title": "OAuth Integration: Token Storage, Provider Configuration, and Token Validity",
  "summary": "Tests for PocketPaw's OAuth subsystem, covering secure token persistence in TokenStore, provider URL generation via OAuthManager, and token validity checks based on expiry timestamps. Verifies file permissions, non-existent token handling, and graceful degradation for unknown providers.",
  "concepts": [
    "OAuth",
    "TokenStore",
    "OAuthManager",
    "token persistence",
    "file permissions",
    "token expiry",
    "provider configuration",
    "access token",
    "refresh token",
    "integrations",
    "credential security"
  ],
  "categories": [
    "integrations",
    "security",
    "OAuth",
    "testing",
    "test"
  ],
  "source_docs": [
    "754d68a1e50eb941"
  ],
  "backlinks": null,
  "word_count": 545,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw integrates with external services (Google Calendar, Gmail, etc.) via OAuth 2.0. The OAuth subsystem has two components: `TokenStore` (encrypted-at-rest token persistence) and `OAuthManager` (authorization URL generation and provider configuration). The test suite validates that tokens are stored securely, loaded correctly, and checked for expiry before use.

## TokenStore (`TestTokenStore`)

`TokenStore` saves and loads `OAuthTokens` objects as JSON files in an OAuth-specific directory (defaulting to `~/.pocketpaw/oauth/`). The fixture monkeypatches `_get_oauth_dir` to a `tmp_path` so tests do not touch the real home directory.

- `test_save_and_load`: A full `OAuthTokens` object (access token, refresh token, expiry, scopes) round-trips through save/load without data loss. This is the core correctness assertion — any serialization error here breaks all OAuth integrations.
- `test_load_nonexistent`: Loading a token for an unknown service returns `None` rather than raising. Callers use `None` as a signal to trigger the OAuth flow.
- `test_delete` / `test_delete_nonexistent`: Tokens can be removed (e.g., on user logout). Deleting a non-existent token returns `False` without raising.
- `test_list_services`: Returns the list of services for which tokens are stored. Used by the dashboard to show connected integrations.
- `test_file_permissions`: After saving, the token file must have mode `600` (owner read/write only). Without this, other users on a shared machine could read OAuth tokens — a credential leak. The test uses `os.stat` to verify permissions, not just that the file exists.

## OAuthManager (`TestOAuthManager`)

- `test_get_auth_url`: For a known provider (e.g., Google), `get_auth_url()` returns a valid authorization URL. The URL must include the provider's auth endpoint and the requested scopes — missing scopes would cause the token to lack required permissions.
- `test_get_auth_url_unknown_provider`: Requesting a URL for an unknown provider raises a clear error rather than returning `None` or a malformed URL. Callers must know immediately if they configured a wrong provider name.
- `test_providers_config`: The `PROVIDERS` dict is non-empty and each entry has the required fields (auth URL, token URL, required keys). This catches a misconfiguration where a provider entry is defined but missing critical fields.

## Token Validity (`TestOAuthTokens`)

- `test_dataclass_fields`: `OAuthTokens` has all expected fields (`service`, `access_token`, `refresh_token`, `expires_at`, `scopes`). A missing field would break any caller that accesses it.
- `test_defaults`: `refresh_token` and `scopes` have sensible defaults (empty string / empty list) so callers do not need to guard against `None`.

## Async Token Retrieval

- `test_get_valid_token_fresh`: `get_valid_token()` returns the access token when `expires_at` is in the future. The expiry check prevents using stale tokens that would cause 401 errors on API calls.
- `test_get_valid_token_not_found`: Returns `None` when no token is stored, signaling the caller to re-initiate OAuth. This is preferable to raising, because OAuth re-auth is a recoverable user action, not a system error.

## Design Notes

The file permission test (`test_file_permissions`) is particularly important in a personal AI assistant context: users connect their Google accounts and expect those credentials to be safe. A `644` permission would expose tokens to any other process running as the same user or any user on a multi-user system.

## Known Gaps

No TODOs in this file. The suite does not test token refresh — the flow where an expired token is automatically exchanged for a new one using the refresh token. That path is untested and is likely handled at a higher layer or deferred to a future sprint.
