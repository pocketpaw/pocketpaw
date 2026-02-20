"""In-memory token-bucket rate limiter for the web dashboard.

Pre-configured tiers:
  - api:      10 req/s, burst 30  (general API endpoints)
  - auth:      1 req/s, burst  5  (token/QR endpoints)
  - ws:        2 conn/s, burst  5  (WebSocket connections)
  - api_key:   configurable per-key limiter (default 60 req/min)

No external dependencies — pure stdlib.
"""

from __future__ import annotations

import math
import time

__all__ = [
    "RateLimiter",
    "RateLimitInfo",
    "api_limiter",
    "auth_limiter",
    "ws_limiter",
    "get_api_key_limiter",
    "cleanup_all",
]


class _Bucket:
    """A single token bucket for one client."""

    __slots__ = ("tokens", "last_refill")

    def __init__(self, capacity: float, now: float):
        self.tokens: float = capacity
        self.last_refill: float = now


class RateLimitInfo:
    """Rate limit state returned by ``check()``."""

    __slots__ = ("allowed", "limit", "remaining", "reset_after")

    def __init__(self, allowed: bool, limit: int, remaining: int, reset_after: float):
        self.allowed = allowed
        self.limit = limit
        self.remaining = remaining
        self.reset_after = reset_after

    def headers(self) -> dict[str, str]:
        """Return rate-limit response headers (RFC 6585 style)."""
        h: dict[str, str] = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(math.ceil(self.reset_after)),
        }
        if not self.allowed:
            h["Retry-After"] = str(math.ceil(self.reset_after))
        return h


class RateLimiter:
    """Token-bucket rate limiter keyed by client identifier (IP address or API key).

    Parameters
    ----------
    rate : float
        Tokens added per second.
    capacity : int
        Maximum burst size (bucket capacity).
    """

    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, key: str) -> bool:
        """Return True if the request is allowed, consuming one token."""
        return self.check(key).allowed

    def check(self, key: str) -> RateLimitInfo:
        """Check rate limit and return detailed info with header values."""
        now = time.monotonic()

        if key not in self._buckets:
            self._buckets[key] = _Bucket(self.capacity, now)

        bucket = self._buckets[key]

        # Refill tokens since last check
        elapsed = now - bucket.last_refill
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.rate)
        bucket.last_refill = now

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            remaining = int(bucket.tokens)
            reset_after = (self.capacity - bucket.tokens) / self.rate if self.rate > 0 else 0
            return RateLimitInfo(True, self.capacity, remaining, reset_after)

        # Denied — compute time until next token
        reset_after = (1.0 - bucket.tokens) / self.rate if self.rate > 0 else 1.0
        return RateLimitInfo(False, self.capacity, 0, reset_after)

    def cleanup(self, max_age: float = 3600.0) -> int:
        """Remove stale entries older than *max_age* seconds. Returns count removed."""
        now = time.monotonic()
        stale = [k for k, b in self._buckets.items() if now - b.last_refill > max_age]
        for k in stale:
            del self._buckets[k]
        return len(stale)


# Pre-configured limiter instances
api_limiter = RateLimiter(rate=10.0, capacity=30)
auth_limiter = RateLimiter(rate=1.0, capacity=5)
ws_limiter = RateLimiter(rate=2.0, capacity=5)
_api_key_limiter: RateLimiter | None = None


def get_api_key_limiter() -> RateLimiter:
    """Return the per-API-key limiter, initialized from config on first call."""
    global _api_key_limiter
    if _api_key_limiter is None:
        try:
            from pocketpaw.config import Settings

            capacity = Settings.load().api_rate_limit_per_key
        except Exception:
            capacity = 60
        _api_key_limiter = RateLimiter(rate=capacity / 60.0, capacity=capacity)
    return _api_key_limiter


def cleanup_all() -> int:
    """Run cleanup on all global limiters. Returns total entries removed."""
    total = api_limiter.cleanup() + auth_limiter.cleanup() + ws_limiter.cleanup()
    if _api_key_limiter is not None:
        total += _api_key_limiter.cleanup()
    return total
