---
{
  "title": "DoS Hardening: Rate Limiter TOCTOU Concurrency and ReDoS Regression Tests",
  "summary": "This security-focused suite reproduces two DoS vulnerability classes found during a 2026-04 security sprint: a TOCTOU race condition in the rate limiter that allows more tokens than the bucket capacity under thread contention, and a ReDoS (Regular Expression Denial of Service) vulnerability where a pathological input causes catastrophic regex backtracking. Both are bug-reproduction tests written before the fixes.",
  "concepts": [
    "RateLimiter",
    "TOCTOU_race",
    "concurrent_check",
    "token_bucket",
    "DoS_hardening",
    "ReDoS",
    "catastrophic_backtracking",
    "threading_concurrency",
    "capacity_enforcement",
    "security_sprint",
    "rate_limiting",
    "regex_vulnerability",
    "security"
  ],
  "categories": [
    "testing",
    "security",
    "rate-limiting",
    "dos-hardening",
    "concurrency",
    "test"
  ],
  "source_docs": [
    "6f319da1e716d0f7"
  ],
  "backlinks": null,
  "word_count": 549,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_dos_hardening.py` was added during security sprint cluster F (April 2026, issues #891 and #895) specifically to reproduce and document two denial-of-service vulnerability classes in PocketPaw's security layer. Following the project's test-first bug-fixing policy, these tests were written to fail against the vulnerable code before any fix was implemented.

## Why This Module Exists

PocketPaw agents are exposed to user-controlled inputs via channel adapters (Discord messages, HTTP requests). Two classes of DoS attacks were identified that could allow a malicious user to either bypass rate limiting (consuming unlimited resources) or freeze the server process (blocking all requests) with a crafted input string.

## #891 — Rate Limiter TOCTOU Race

### The Vulnerability

`TestRateLimiterConcurrency.test_concurrent_check_does_not_exceed_capacity` reproduces a classic check-time-to-use-time (TOCTOU) race in the rate limiter's `check()` method. The original implementation used a non-atomic pattern:

```python
# Vulnerable pattern (pseudocode):
if self.tokens > 0:       # check
    self.tokens -= 1      # use (separate operation)
    return allowed
```

Under concurrent load, two threads can both pass the `if self.tokens > 0` check when only one token remains, then both decrement — allowing two requests through with only one token.

### The Test

The test creates a `RateLimiter` with `capacity=10` and launches 200 concurrent threads, each calling `limiter.check("client-x")`. If more than 10 are marked `allowed`, the race is confirmed. The assertion enforces `allowed <= 10`.

Using 200 threads against a capacity of 10 provides enough contention to reliably expose the race on most hardware. The `counter_lock` in the test is a separate threading lock protecting only the test's counter variable — it is not the fix for the limiter itself.

### Why It Matters

Without a lock inside the rate limiter, a high-concurrency attack (e.g., 200 simultaneous Discord messages) could drain the token bucket and overflow it, allowing unlimited throughput past the rate limit. For a bot exposed to a public Discord server, this is a realistic attack vector.

## #895 — ReDoS Regression

### The Vulnerability

The second test class (not fully shown in the source excerpt) reproduces a Regular Expression Denial of Service. Certain regex patterns with nested quantifiers exhibit catastrophic backtracking on pathological inputs — processing time grows exponentially with input length. A single malicious message of ~50 characters could peg the Python thread at 100% CPU for seconds, blocking all other requests.

### The Test Approach

The test crafts a known-bad input string and asserts that the regex operation completes within a strict time budget (typically under 100ms). If the regex hangs, the test times out and fails, confirming the vulnerability.

### Why It Matters

Python's `re` module uses a backtracking engine and does not have built-in ReDoS protection. Any user-controlled string that passes through a vulnerable regex is a potential freeze vector. The fix typically involves rewriting the pattern to avoid nested quantifiers or using a linear-time regex library.

## Test Infrastructure

Both tests are self-contained with no external fixtures. The threading test uses Python's standard `threading.Thread` rather than `asyncio` because the rate limiter operates at the synchronous layer (called before async request handlers). The ReDoS test uses `time.time()` for timing assertions.

## Known Gaps

The rate limiter test does not verify behavior under asyncio concurrency (coroutines rather than threads) — if the rate limiter is also called from async contexts, a separate asyncio-based concurrency test would be needed.
