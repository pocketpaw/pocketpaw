"""In-memory token-bucket rate limiter for the web dashboard.

Three pre-configured tiers:
  - api:  10 req/s, burst 30  (general API endpoints)
  - auth:  1 req/s, burst  5  (token/QR endpoints)
  - ws:    2 conn/s, burst  5  (WebSocket connections)

No external dependencies — pure stdlib.
"""

import time

__all__ = [
    "RateLimiter",
    "api_limiter",
    "auth_limiter",
    "ws_limiter",
    "cleanup_all",
]


class _Bucket:
    """A single token bucket for one client."""

    __slots__ = ("tokens", "last_refill")

    def __init__(self, capacity: float, now: float):
        self.tokens: float = capacity
        self.last_refill: float = now


class RateLimiter:
    """Token-bucket rate limiter keyed by client identifier (IP address).

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
            return True
        return False

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


def cleanup_all() -> int:
    """Run cleanup on all global limiters. Returns total entries removed."""
    return api_limiter.cleanup() + auth_limiter.cleanup() + ws_limiter.cleanup()
