---
{
  "title": "Drive OAuth Bearer Token Resolution: Credential Broker, Env Var, and OAuthManager Fallback Chain",
  "summary": "`auth.py` implements a prioritised three-source token-resolution chain so the Drive connector always has a valid OAuth bearer token regardless of environment (production, CI, local dev). It isolates token concerns from `source.py`, keeping the adapter code focused on business logic.",
  "concepts": [
    "OAuth",
    "bearer token",
    "credential broker",
    "OAuthManager",
    "TokenStore",
    "event loop isolation",
    "lazy import",
    "GOOGLE_OAUTH_TOKEN",
    "DriveAuthError",
    "token precedence chain"
  ],
  "categories": [
    "authentication",
    "connectors",
    "Google Drive"
  ],
  "source_docs": [
    "b125386c5a941ae4"
  ],
  "backlinks": null,
  "word_count": 447,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`auth.py` provides `resolve_bearer_token`, the single entry-point for obtaining a Google Drive OAuth bearer token anywhere in the connector layer. The function exists because bearer tokens can come from multiple sources depending on context — production routing, local development, headless CI — and the Drive adapter needs them all without duplicating logic.

## Token Precedence Chain

The function resolves the first non-empty token in this order:

```
1. Credential.token  (from the retrieval router's credential broker)
2. GOOGLE_OAUTH_TOKEN  (environment variable — local dev / CI)
3. OAuthManager  (existing token store from the OAuth flow in the dashboard)
```

**Why this order?** The broker credential is the most tightly scoped — it is a short-lived token issued specifically for this dispatch, so it should always win. The env var is a developer escape hatch that avoids any async roundtrip, making tests and CI fast. The OAuthManager is the production fallback for the `drive_*` built-in tools that use the dashboard's OAuth flow rather than the credential broker.

## Defensive Patterns

### Lazy Imports for the OAuth Store

The `OAuthManager` and related config imports are deferred inside the function body:

```python
try:
    from pocketpaw.config import get_settings
    from pocketpaw.integrations.oauth import OAuthManager
    from pocketpaw.integrations.token_store import TokenStore
except Exception as e:
    raise DriveAuthError(...) from e
```

This is intentional. The `integrations` package loads config state eagerly, which would slow down pure-library imports of the `drive` package even when the OAuth path is never exercised. Lazy import keeps the module cheap to load.

### Isolated Event Loop for the Async Token Fetch

```python
loop = asyncio.new_event_loop()
try:
    token = loop.run_until_complete(_fetch())
finally:
    loop.close()
```

`OAuthManager.get_valid_token` is async, but `SourceAdapter.query` is sync (soul-protocol 0.3.1 runs adapters on a thread pool). Using `asyncio.new_event_loop()` rather than `asyncio.run()` prevents the function from swapping out the main-thread default event loop on teardown — a subtle bug that would break any downstream code calling `asyncio.get_event_loop()`.

### Short-Circuit for Empty Token Store

Before creating the async loop, the code checks `TokenStore.load("google_drive")` synchronously. If the store is empty (fresh install, unauthenticated), it raises immediately with a clear user-facing message instead of spinning up an event loop just to get `None`. This keeps tests fast and error messages actionable.

### `env` Injection for Tests

The function accepts an optional `env: dict[str, str] | None` parameter that defaults to `os.environ`. This lets tests inject arbitrary environment variables without monkeypatching the module, making test isolation trivial.

## Known Gaps

- **Lazy token refresh on long-running dispatches**: The TODO comment notes that `OAuthManager` refreshes lazily on access. For large parallel batches, the first worker may pay the full refresh latency while other workers wait. The comment defers a warm-up phase until the Salesforce adapter confirms the pattern.
