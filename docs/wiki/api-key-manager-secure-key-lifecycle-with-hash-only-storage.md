---
{
  "title": "API Key Manager: Secure Key Lifecycle with Hash-Only Storage",
  "summary": "`APIKeyManager` provides create, verify, revoke, rotate, and list operations for API keys using a GitHub PAT-style design: keys are shown once at creation and only their SHA-256 hash is persisted. Keys use the `pp_` prefix for log identification, support fine-grained scopes, and are stored in `~/.pocketpaw/api_keys.json`.",
  "concepts": [
    "API keys",
    "SHA-256 hashing",
    "scope-based authorization",
    "pp_ prefix",
    "file persistence",
    "APIKeyRecord",
    "hmac.compare_digest",
    "timing attack prevention",
    "singleton pattern",
    "key rotation"
  ],
  "categories": [
    "api",
    "security",
    "authentication",
    "key management"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 456,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's API layer needs a way for automation scripts and the Tauri desktop app to authenticate without requiring the user to be logged in or to expose their master token. API keys fill this role: they are long-lived, revocable, scope-limited credentials that external clients include in request headers.

## Key Format and Prefix

Keys follow the format `pp_<32-char-random>`. The `pp_` prefix serves a specific operational purpose: when a key appears in server logs, CI output, or error reports, the prefix makes it immediately recognizable as a PocketPaw API key rather than a random string. This mirrors GitHub's `ghp_` prefix design and makes automated secret scanning tools easier to configure.

The `_hash_key` function applies SHA-256 to the full key before storage:

```python
def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
```

The plaintext key is returned to the caller exactly once at creation time and is never written to disk. This means a database leak exposes only hashes, which cannot be reversed to recover valid keys.

## Scope System

`VALID_SCOPES` defines the permission model:

```python
VALID_SCOPES = frozenset({"chat", "sessions", "settings:read", "settings:write",
                           "channels", "memory", "admin"})
```

Each key carries a subset of these scopes. The `require_scope` dependency in `deps.py` enforces that the key's scope set intersects the required scopes for each endpoint. The `admin` scope is a wildcard that grants access to everything, intended for the Tauri app acting on behalf of the local user.

## File-Based Persistence

`APIKeyRecord` (a Pydantic BaseModel) stores key metadata without the plaintext:

- `key_hash` — SHA-256 digest for verification
- `prefix` — First 8 characters for log identification without exposing the full key
- `scopes`, `created_at`, `last_used_at`, `expires_at`, `revoked`

Records are serialized to `~/.pocketpaw/api_keys.json`. The `_load` and `_save` methods handle JSON serialization with atomic-ish writes (write to temp file, rename) to prevent corruption on crash. HMAC comparison (`hmac.compare_digest`) is used during verification to prevent timing attacks that could reveal whether a submitted key prefix matches a stored hash.

## Singleton Pattern and Test Reset

`get_api_key_manager()` returns a module-level singleton `APIKeyManager` instance, avoiding repeated file reads across requests. `reset_api_key_manager()` clears the singleton for tests, a pattern that ensures each test starts with a clean state without needing dependency injection throughout the call stack.

## Known Gaps

- Key rotation is not atomic: the old key is revoked and a new one created in two separate file writes. A crash between the two writes could leave a user with no valid key.
- `last_used_at` is updated on every verification, which means every authenticated request triggers a file write. Under high request rates, this could become a bottleneck.
- The storage format is a flat JSON array; with many keys, linear scan on every verification becomes noticeable. A future version should index by `key_hash`.
