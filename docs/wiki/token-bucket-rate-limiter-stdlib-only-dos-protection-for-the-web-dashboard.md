---
{
  "title": "Token-Bucket Rate Limiter: Stdlib-Only DoS Protection for the Web Dashboard",
  "summary": "This module implements an in-memory token-bucket rate limiter using only Python's standard library, providing pre-configured tiers for API endpoints, authentication, WebSocket connections, and per-API-key limiting. The zero-dependency design makes it suitable for embedding directly in the dashboard without adding external Redis or other state store requirements.",
  "concepts": [
    "rate limiting",
    "token bucket algorithm",
    "RateLimiter",
    "RateLimitInfo",
    "DoS protection",
    "brute force prevention",
    "WebSocket limiting",
    "thread safety",
    "per-API-key limiting",
    "cleanup",
    "stdlib-only"
  ],
  "categories": [
    "security",
    "api",
    "web dashboard"
  ],
  "source_docs": [
    "6bd4366b9ceeaa99"
  ],
  "backlinks": null,
  "word_count": 515,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Why Rate Limiting Matters for an Agent Runtime

The PocketPaw dashboard exposes API endpoints that trigger LLM calls, tool executions, and memory operations — all of which consume real resources and may carry per-call cost. Without rate limiting, a bug in a client, a misconfigured automation, or a deliberate denial-of-service attempt can exhaust API quotas, overwhelm the agent backend, or drive up cloud bills.

Authentication endpoints face a distinct threat: brute-force credential stuffing. A `1 req/s` limit on `/token` and QR endpoints makes automated password guessing economically infeasible.

## Token Bucket Algorithm

The token bucket algorithm was chosen over simpler fixed-window counters because it handles burst traffic gracefully. A client that has been idle accumulates tokens up to the bucket's `capacity`, then can spend that burst all at once. This prevents false-positive throttling of bursty-but-legitimate clients (e.g., a dashboard that fires multiple requests on page load) while still enforcing the average rate over time.

The `_Bucket` class tracks `tokens` and `last_refill` timestamp. On each `allow()` call, elapsed time is converted to new tokens (`elapsed * rate`), capped at `capacity`, then one token is consumed.

## Pre-Configured Tiers

```
api:      10 req/s, burst 30   — general API endpoints
auth:      1 req/s, burst  5   — token/QR endpoints  
ws:        2 conn/s, burst  5  — WebSocket connections
api_key:   configurable         — per-API-key limiting (default 60 req/min)
```

The `auth` tier's extremely conservative limit (1 req/s) reflects the threat model: authentication endpoints are the highest-value brute-force target. The `ws` tier limits connection establishment, not message throughput, because WebSocket upgrade requests are more expensive than ongoing message frames.

## RateLimitInfo: Structured Response

The `check()` method returns a `RateLimitInfo` object rather than a bare boolean. This carries `allowed`, `limit`, `remaining`, and `reset_after` — enough information to populate standard HTTP rate limit headers (`X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After`), which well-behaved clients use to back off automatically.

## Thread Safety

The `threading` module import signals that the limiter uses locking to protect the bucket map against concurrent access from FastAPI's async workers running in a thread pool. Without locks, two simultaneous requests for the same key could both read `tokens > 0` and both decrement, allowing twice the intended throughput.

## Memory Cleanup

`cleanup(max_age)` evicts bucket entries that have not been accessed within `max_age` seconds. Without this, the in-memory bucket map grows unbounded as unique client IPs accumulate. `cleanup_all()` is the global convenience function that runs cleanup across all pre-configured limiters.

## Per-API-Key Limiter

`get_api_key_limiter()` initializes the per-key limiter from application config on first call, allowing operators to tune the default rate for API key clients without modifying code.

## Known Gaps

- **Not distributed**: The rate limiter state lives in a single process's memory. If PocketPaw runs behind a load balancer with multiple workers, each worker maintains independent buckets — effectively multiplying the allowed rate by the number of workers.
- **No persistent state across restarts**: Restarting the process resets all buckets. A client that has been rate-limited can bypass the limit by causing a restart.
- **No configurable tier rates at runtime**: Tier rates are set at instantiation. Changing them requires a process restart.