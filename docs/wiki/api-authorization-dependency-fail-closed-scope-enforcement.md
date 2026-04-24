---
{
  "title": "API Authorization Dependency: Fail-Closed Scope Enforcement",
  "summary": "`require_scope` is a FastAPI dependency that enforces fail-closed authorization across all API endpoints. It accepts requests only when the caller has been explicitly marked as fully trusted, carries an API key with the required scope, or carries an OAuth2 token with the required scope — rejecting all other requests with HTTP 403.",
  "concepts": [
    "require_scope",
    "fail-closed authorization",
    "FastAPI dependency",
    "full_access",
    "scope checking",
    "API key scopes",
    "OAuth2 token scopes",
    "403 rejection",
    "testing escape hatch",
    "HTTP 403"
  ],
  "categories": [
    "api",
    "security",
    "authorization",
    "FastAPI"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 428,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`deps.py` provides the single reusable authorization mechanism for the entire PocketPaw API layer. Rather than each endpoint implementing its own auth check, all protected routes declare their requirements once via `Depends(require_scope("scope_name"))`.

## Fail-Closed Design

The critical design decision, documented in a changelog comment, is that the dependency is **fail-closed**:

> `require_scope` now fails closed. Master/session/cookie/localhost auth must set `request.state.full_access = True` explicitly — no implicit bypass. Closes #888.

Before this change (pre-2026-04-16), certain auth paths may have implicitly bypassed the scope check. The fix requires every trusted auth path in `dashboard_auth.py` to explicitly set `request.state.full_access = True`. If a new auth path is added and the developer forgets to set `full_access`, the request will be rejected rather than accidentally allowed through. This is the correct default for security-sensitive code.

## Authorization Logic

The dependency checks three authorization sources in order:

1. **`request.state.full_access`** — Set by master token, session token, cookie auth, and localhost auth paths. Grants unrestricted access to all scopes.
2. **`request.state.api_key`** — An `APIKeyRecord` attached by API key middleware. The key's scope set must contain one of the required scopes or `admin`.
3. **`request.state.oauth_token`** — An `OAuthToken` attached by OAuth2 middleware. The token's scope string must contain one of the required scopes or `admin`.

If none of these conditions holds, the dependency raises `HTTPException(status_code=403)`.

## Usage Pattern

```python
@router.put("/settings", dependencies=[Depends(require_scope("settings:write"))])
async def update_settings(...):
    ...
```

Multiple scopes can be passed — the check passes if the caller has **any** of the listed scopes (OR logic, not AND). This supports use cases like "this endpoint is accessible to both `admin` and `settings:write` scope holders."

## Testing Escape Hatch

```python
_TESTING_FULL_ACCESS: bool = False
```

This module-level flag is set to `True` by `tests/v1/conftest.py` so that tests mounting individual routers without the full dashboard middleware stack can exercise route logic without installing middleware in every fixture. The comment explicitly states it is "always False in production" — there is no runtime mechanism that sets it to True outside of test code.

This is a pragmatic choice: the alternative (injecting mock auth state into every test request) would make test fixtures significantly more complex and would couple tests to the auth middleware implementation.

## Known Gaps

- The `_TESTING_FULL_ACCESS` flag is a global mutable, which can cause test pollution if tests run in parallel and one test sets the flag while another relies on it being False. Thread-safety is not documented.
- There is no audit logging when a request is rejected by `require_scope`, making it difficult to diagnose auth failures in production without enabling debug-level logging.
