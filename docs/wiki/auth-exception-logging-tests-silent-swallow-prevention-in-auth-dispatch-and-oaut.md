---
{
  "title": "Auth Exception Logging Tests: Silent Swallow Prevention in Auth Dispatch and OAuth2",
  "summary": "Regression tests for issue #627 — verifies that bare `except Exception: pass` blocks in authentication dispatch paths have been replaced with `logger.warning(..., exc_info=True)` calls. Tests cover API key validation, OAuth2 token validation, cookie login branches, and OAuth2 server audit log failures, ensuring auth failures are always logged and always return 401.",
  "concepts": [
    "exception logging",
    "silent exception swallowing",
    "exc_info=True",
    "logger.warning",
    "_auth_dispatch",
    "cookie_login",
    "OAuth2 exception handling",
    "audit log fault tolerance",
    "issue #627",
    "PKCE",
    "401 response"
  ],
  "categories": [
    "testing",
    "authentication",
    "error handling",
    "security",
    "test"
  ],
  "source_docs": [
    "c8e751705333e538"
  ],
  "backlinks": null,
  "word_count": 479,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_auth_exception_logging.py` addresses a class of defensive programming bug: silent exception swallowing in security-critical code paths. Issue #627 identified several `except Exception: pass` blocks in `dashboard_auth.py` and `api/oauth2/server.py` that discarded exceptions without logging them. This file proves the fix — replacing silent `pass` with `logger.warning(..., exc_info=True)` — is in place and cannot regress.

## Why Silent Exception Swallowing is Dangerous

In authentication code, a silent exception typically causes the auth check to fail "closed" (returning 401) rather than "open" (granting access). So the immediate security impact is often just a user seeing an unexpected 401 error. However, the silent failure:

1. **Hides bugs** — a misconfigured key validator raising `AttributeError` looks the same as a legitimately invalid key, making incidents invisible.
2. **Blocks incident response** — if an attacker is probing the system with malformed tokens, silent failures produce no log evidence.
3. **Makes on-call debugging impossible** — without `exc_info=True`, the stack trace is lost.

## _auth_dispatch: API Key Exception Logging

`TestAuthDispatchApiKeyExceptionLogging` patches the API key verifier to raise a `RuntimeError` and verifies:

- `test_api_key_exception_is_logged` — `logger.warning` is called with `exc_info=True`.
- `test_api_key_exception_still_returns_401` — the request still receives a 401 response; the exception does not grant access.

## _auth_dispatch: OAuth2 Exception Logging

`TestAuthDispatchOAuth2ExceptionLogging` patches the OAuth2 token verifier to raise and verifies the same two properties: the warning is logged, and the response is still 401.

## cookie_login: OAuth2 Exception Logging

`TestCookieLoginOAuth2ExceptionLogging` covers the OAuth2 branch of the `cookie_login` function. Cookie-based login is a separate code path from the middleware auth dispatch, so it requires its own coverage.

## cookie_login: API Key Exception Logging

`TestCookieLoginApiKeyExceptionLogging` covers the API key branch of `cookie_login`. Same pattern: exception is logged with `exc_info=True`, response is 401.

## OAuth2 Server Audit Log Exception Logging

`TestOAuth2ServerAuditLogExceptionLogging` covers a different failure mode: the audit log write inside `AuthorizationServer.exchange()` raises an exception. The tests verify:

- `test_audit_log_failure_is_logged` — the write failure is logged as a warning.
- `test_audit_log_failure_does_not_block_token_exchange` — the token exchange succeeds even when the audit write fails. The audit log must not be in the critical path of authentication — blocking a legitimate token exchange because the audit database is temporarily unavailable would lock out users.

The `_setup_server` helper in this test class creates an `AuthorizationServer` with a full PKCE flow, advancing to the point where `exchange()` is called and the audit write is triggered.

## Fixture Design

`auth_test_client` creates a `TestClient` for the full dashboard app. The black-box tests (returning 401) use this client, while the logger assertion tests use direct async calls with mocked loggers to precisely capture `logger.warning` invocations.

`_make_pkce_pair()` is reused from the OAuth2 test patterns to produce valid PKCE credentials for setting up the exchange flow.

## Known Gaps

No TODO or FIXME markers. The test module does not cover exception logging in the WebSocket auth path or in channel-specific auth handlers, which may have similar exception swallowing patterns.