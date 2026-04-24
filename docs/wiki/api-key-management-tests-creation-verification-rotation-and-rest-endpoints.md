---
{
  "title": "API Key Management Tests: Creation, Verification, Rotation, and REST Endpoints",
  "summary": "Comprehensive tests for PocketPaw's `APIKeyManager` class and its REST endpoints covering the full key lifecycle — creation with scope validation, verification, revocation, rotation, and expiry rejection. The test suite isolates all disk I/O to a temp directory to prevent cross-test contamination and ensure file permission checks work correctly.",
  "concepts": [
    "APIKeyManager",
    "API key lifecycle",
    "key rotation",
    "key revocation",
    "scope validation",
    "key verification",
    "file permissions",
    "pp_ prefix",
    "key expiry",
    "REST endpoints",
    "monkeypatching"
  ],
  "categories": [
    "testing",
    "authentication",
    "API key management",
    "security",
    "test"
  ],
  "source_docs": [
    "48ff5cb93ce016e8"
  ],
  "backlinks": null,
  "word_count": 489,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_api_keys.py` covers PocketPaw's programmatic API key system. API keys are how external tools, agent runners, and custom integrations authenticate with PocketPaw without requiring interactive login. The key lifecycle — creation, verification, revocation, and rotation — has strict correctness requirements because a bug in any of these operations could silently grant unauthorized access or lock out legitimate callers.

## APIKeyManager Unit Tests

`TestAPIKeyManager` tests the storage-layer class directly, bypassing HTTP. Each test gets its own `APIKeyManager` instance pointing at a fresh temp directory, preventing tests from sharing state.

### Key Creation

`test_create_key` verifies a freshly created key has the expected structure. `test_create_key_custom_scopes` confirms that callers can specify a restricted scope list (e.g., `["read"]` instead of the default full scope set). `test_create_key_invalid_scopes` confirms the manager rejects unrecognized scope strings rather than silently accepting them — accepting garbage scopes would create keys that claim permissions the system never defined.

### Verification

`test_verify_valid_key` proves a freshly created key verifies successfully. `test_verify_invalid_key` confirms a random string returns a failure result. `test_verify_non_pp_key` tests a key that looks syntactically valid but does not have the `pp_` prefix that PocketPaw uses to namespace its keys — the manager must reject it without crashing.

### Expiry

`test_expired_key_rejected` creates a key with an expiration in the past and confirms verification fails. This prevents a class of bug where expiry is checked at creation time but not at verification time.

### Revocation and Idempotency

`test_revoke_key` confirms a key is unreachable after revocation. `test_revoke_nonexistent` verifies revocation of a non-existent key ID raises an appropriate error rather than silently succeeding. `test_revoke_already_revoked` tests the idempotency edge: revoking an already-revoked key should not corrupt state or raise an unexpected exception.

### Rotation

`test_rotate_key` is the atomicity check: after rotation, the new key verifies successfully and the old key is implicitly revoked. `test_rotate_nonexistent` verifies graceful error handling when rotating an ID that doesn't exist.

### File Permissions

`test_file_permissions` asserts the key storage file is created with restrictive permissions (not world-readable). Storing API keys in a world-readable file would allow any process on the same machine to read all credentials.

## REST Endpoint Tests

`TestAPIKeyEndpoints` wraps the manager in a FastAPI test app (using `monkeypatch` to inject the temp-dir manager as the module-level singleton) and tests all CRUD endpoints over HTTP.

The endpoint tests verify:
- `POST /api_keys` creates a key and returns a plaintext secret in the response (only shown once).
- The default scope list is applied when no scopes are specified.
- Invalid scopes return a 422 response before reaching the manager.
- `GET /api_keys` lists all keys without returning plaintext secrets.
- `DELETE /api_keys/{id}` revokes the key (404 on non-existent).
- `POST /api_keys/{id}/rotate` returns a new plaintext secret (404 on non-existent).

## Known Gaps

No explicit TODO or FIXME markers. The test suite does not cover concurrent rotation (two requests rotating the same key simultaneously), which could theoretically produce two valid keys if the manager's file write is not atomic.