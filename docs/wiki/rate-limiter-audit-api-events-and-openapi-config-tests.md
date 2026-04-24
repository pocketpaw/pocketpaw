---
{
  "title": "Rate Limiter, Audit API Events, and OpenAPI Config Tests",
  "summary": "Tests PocketPaw's token-bucket rate limiter, the `RateLimitInfo` response model and its header formatting, audit logging for API events (OAuth and key revocation), and the OpenAPI endpoint configuration. Together these tests ensure the API protects against request floods, produces correct rate limit headers, and maintains a searchable audit trail.",
  "concepts": [
    "rate limiter",
    "token bucket",
    "RateLimitInfo",
    "X-RateLimit headers",
    "Retry-After",
    "audit logging",
    "JSONL audit trail",
    "get_api_key_limiter",
    "OpenAPI config",
    "api_rate_limit_per_key",
    "RFC 6585"
  ],
  "categories": [
    "testing",
    "security",
    "rate limiting",
    "audit",
    "test"
  ],
  "source_docs": [
    "b4cf5175f1c1edc2"
  ],
  "backlinks": null,
  "word_count": 461,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_api_rate_limits.py` covers three distinct but related concerns: the rate limiting infrastructure that protects PocketPaw's API from overuse, the `RateLimitInfo` model that encodes decisions into HTTP headers, and API-level audit logging for security-sensitive operations.

## RateLimiter Tests

`TestRateLimiter` tests the `RateLimiter` class from `pocketpaw.security.rate_limiter`, which implements a token-bucket algorithm.

### check() Return Type

`test_check_returns_info` verifies `check()` returns a `RateLimitInfo` object (not a plain boolean). This matters because callers need the full decision context — remaining tokens, limit, reset time — to construct proper HTTP response headers and decide whether to log the event.

### Bucket Exhaustion

`test_check_denied` creates a limiter with `rate=0.1` (very slow refill) and `capacity=2`, calls `check()` three times, and verifies the third call returns `allowed=False` with `remaining=0`. This proves the token-bucket deduction logic works and does not allow overcommit.

### HTTP Headers

`test_headers_on_allowed` and `test_headers_on_denied` verify the `headers()` method produces the standard RFC 6585 rate limit headers:
- `X-RateLimit-Limit` — bucket capacity
- `X-RateLimit-Remaining` — tokens left
- `X-RateLimit-Reset` — epoch time when bucket refills

On a denied request, `Retry-After` should be present (for client backoff). On an allowed request, `Retry-After` must be absent. This asymmetry prevents clients from miscalculating backoff timing.

### API Key Limiter

`test_api_key_limiter_exists` confirms `get_api_key_limiter()` returns a pre-configured `RateLimiter` instance. This validates the module-level singleton pattern that the API key middleware uses to throttle per-key traffic.

## RateLimitInfo Tests

`TestRateLimitInfo` exercises the header formatting logic in isolation:
- `test_headers_format` — allowed case, all three standard headers present.
- `test_headers_denied_format` — denied case, `Retry-After` added.

These isolated tests make it easier to diagnose header formatting bugs without needing to exhaust a real bucket.

## Audit API Event Tests

`TestAuditAPIEvents` tests three specific audit logging scenarios:

- `test_log_api_event` — a generic API event is written to the audit log file as JSONL.
- `test_log_api_event_oauth` — an OAuth-specific event (token grant) is audited with OAuth-specific fields.
- `test_log_api_event_revoke` — a revocation event is audited.

All three tests use `tmp_path` to write logs to a temp file, then read back and parse the JSONL to assert field presence. This proves the audit trail cannot be silently dropped or corrupted during normal API operation.

## OpenAPI Config Tests

`TestOpenAPIConfig` verifies the `/api/v1/openapi.json` endpoint exists and returns metadata matching PocketPaw's product identity (title, version). This is a lightweight check that the FastAPI app is correctly configured for API documentation generation.

## Config Field Tests

`TestConfigRateLimit` verifies `api_rate_limit_per_key` exists as a field in PocketPaw's settings model and has a sensible default value. This prevents the config from accidentally losing the field in a Pydantic model refactor.

## Known Gaps

No TODO or FIXME markers. The rate limiter tests use fixed `rate` and `capacity` values rather than production defaults, so regressions in the default configuration values would not be caught here.