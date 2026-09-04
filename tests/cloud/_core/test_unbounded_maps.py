# Regression: every in-process map on a request path must be bounded.
#
# Created 2026-09-04. Four maps grew without limit, three of them reachable
# before authentication. None of these is visible from a response — the symptom
# is a container that grows until it is restarted, and the only recovery in the
# current deploy is `restart: unless-stopped`.
#
#   H3  TimingMiddleware keyed on the raw URL when no route matched, so every
#       distinct 404 minted a permanent 10k-slot deque.
#   M4  The realtime audience cache and the /tree cache both checked a TTL on
#       read and never removed anything, so entries accumulated for the life of
#       the process. The audience cache also had no single-flight, so N
#       concurrent events for one group issued N identical queries.
#   M5  cleanup_all() swept a hand-written list of the six limiters defined in
#       the OSS module. The three cloud limiters were never swept, and could
#       not have been added: the OSS core is forbidden to import pocketpaw_ee.
#
# What each test would catch, stated so a future reader can check the test
# rather than trust it — see the repo's mutation rule:
#   - revert the UNMATCHED_PATH fallback to request.url.path  -> first class
#   - drop the popitem(last=False) loops                      -> the LRU classes
#   - drop the _inflight single-flight                        -> fetch-count test
#   - restore the hand-written cleanup_all list               -> registry test
#   - read forwarded.split(",")[0] again                      -> proxy class

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud._core import timing
from pocketpaw_ee.cloud._core.rate_limit import _client_ip
from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver

from pocketpaw.security.rate_limiter import (
    RateLimiter,
    cleanup_all,
    registered_limiter_count,
)


class _Req:
    """Minimal stand-in for the two attributes _client_ip reads."""

    def __init__(self, peer: str | None, headers: dict[str, str] | None = None):
        self.client = type("C", (), {"host": peer})() if peer else None
        self.headers = headers or {}


class TestTimingKeySpace:
    def setup_method(self):
        timing.reset_buffers()

    def teardown_method(self):
        timing.reset_buffers()

    @pytest.mark.asyncio
    async def test_unmatched_paths_share_one_bucket(self):
        """A scanner walking distinct URLs must not grow the buffer map."""
        mw = timing.TimingMiddleware.__new__(timing.TimingMiddleware)
        mw.capacity = 16

        async def _call_next(_request):
            return type("R", (), {"status_code": 404})()

        for i in range(500):
            req = type(
                "Rq",
                (),
                {
                    "scope": {},  # no matched route
                    "url": type("U", (), {"path": f"/nope/{i}"})(),
                    "method": "GET",
                },
            )()
            await timing.TimingMiddleware.dispatch(mw, req, _call_next)

        assert len(timing._buffers) == 1, (
            f"500 distinct unmatched paths produced {len(timing._buffers)} buffers"
        )
        assert ("GET", timing.UNMATCHED_PATH) in timing._buffers

    @pytest.mark.asyncio
    async def test_matched_routes_still_key_on_the_template(self):
        """The bound must not cost the signal: real routes stay separable."""
        mw = timing.TimingMiddleware.__new__(timing.TimingMiddleware)
        mw.capacity = 16

        async def _call_next(_request):
            return type("R", (), {"status_code": 200})()

        for path in ("/workspaces/{id}", "/pockets", "/pockets/{id}"):
            req = type(
                "Rq",
                (),
                {
                    "scope": {"route": type("Rt", (), {"path": path})()},
                    "url": type("U", (), {"path": "/irrelevant"})(),
                    "method": "GET",
                },
            )()
            await timing.TimingMiddleware.dispatch(mw, req, _call_next)

        assert len(timing._buffers) == 3
        assert ("GET", timing.UNMATCHED_PATH) not in timing._buffers


class TestAudienceCache:
    @pytest.mark.asyncio
    async def test_cache_is_bounded(self):
        async def members(key: str) -> list[str]:
            return [f"u-{key}"]

        r = AudienceResolver(
            workspace_members=members, cache_ttl_seconds=60.0, cache_max_entries=50
        )
        for i in range(500):
            await r._workspace(f"ws-{i}")

        assert len(r._cache) == 50, f"cache grew to {len(r._cache)}"

    @pytest.mark.asyncio
    async def test_eviction_is_least_recently_used(self):
        async def members(key: str) -> list[str]:
            return [f"u-{key}"]

        r = AudienceResolver(workspace_members=members, cache_ttl_seconds=60.0, cache_max_entries=3)
        for k in ("a", "b", "c"):
            await r._workspace(k)
        await r._workspace("a")  # 'a' becomes most recent, so 'b' is coldest
        await r._workspace("d")

        keys = {k for _kind, k in r._cache}
        assert keys == {"a", "c", "d"}, f"evicted the wrong entry: kept {keys}"

    @pytest.mark.asyncio
    async def test_concurrent_misses_issue_one_query(self):
        """Single-flight. This resolver runs on every workspace-scoped event,
        so a fan-out of identical concurrent misses is the normal case."""
        calls = 0
        release = asyncio.Event()

        async def slow_members(key: str) -> list[str]:
            nonlocal calls
            calls += 1
            await release.wait()
            return ["u1"]

        r = AudienceResolver(workspace_members=slow_members, cache_ttl_seconds=60.0)
        waiters = [asyncio.create_task(r._workspace("ws-1")) for _ in range(25)]
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(*waiters)

        assert calls == 1, f"25 concurrent misses issued {calls} queries"
        assert all(res == ["u1"] for res in results)

    @pytest.mark.asyncio
    async def test_a_failed_fetch_is_not_cached_and_does_not_wedge(self):
        """One transient error must not pin a failing entry in front of callers."""
        attempts = 0

        async def flaky(key: str) -> list[str]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("mongo hiccup")
            return ["u1"]

        r = AudienceResolver(workspace_members=flaky, cache_ttl_seconds=60.0)
        with pytest.raises(RuntimeError):
            await r._workspace("ws-1")

        assert r._inflight == {}, "failed fetch left an entry in the in-flight map"
        assert await r._workspace("ws-1") == ["u1"], "retry after a failure did not work"


class TestLimiterRegistry:
    def test_a_new_limiter_is_swept_without_being_registered_by_hand(self):
        """The defect was a hand-written list that could not name the cloud
        limiters, because the OSS core may not import pocketpaw_ee."""
        before = registered_limiter_count()
        limiter = RateLimiter(rate=1.0, capacity=5)
        assert registered_limiter_count() == before + 1

        limiter.check("some-key")
        assert len(limiter._buckets) == 1

        # A negative max_age makes every bucket stale regardless of clock
        # granularity. Windows monotonic ticks at ~15.6ms, so `now - last_refill
        # > 0` is false for a bucket created in the same tick and max_age=0
        # would sweep nothing.
        removed = cleanup_all(max_age=-1.0)
        assert removed >= 1
        assert limiter._buckets == {}, "cleanup_all did not reach a limiter it never named"

    def test_the_cloud_limiters_are_registered(self):
        """The three that were previously unreachable by the sweep."""
        import pocketpaw_ee.cloud._core.rate_limit as rl

        for name in (
            "_invite_create_limiter",
            "_invite_resend_limiter",
            "_social_exchange_limiter",
        ):
            limiter = getattr(rl, name)
            limiter.check(f"probe:{name}")
            assert limiter._buckets, f"{name} did not record a bucket"

        cleanup_all(max_age=-1.0)

        for name in (
            "_invite_create_limiter",
            "_invite_resend_limiter",
            "_social_exchange_limiter",
        ):
            limiter = getattr(rl, name)
            assert limiter._buckets == {}, f"{name} was not swept"


class TestForwardedForTrust:
    """The bucket key must be the address the proxy observed, not the one the
    caller claimed. Getting this wrong is a rate-limit bypass, not just a leak:
    a fresh value per request gets a fresh bucket."""

    def test_rightmost_entry_wins(self):
        req = _Req("10.0.0.1", {"x-forwarded-for": "1.2.3.4, 203.0.113.9"})
        assert _client_ip(req) == "203.0.113.9"

    def test_a_spoofed_leftmost_value_is_ignored(self):
        spoofed = _Req("10.0.0.1", {"x-forwarded-for": "9.9.9.9, 203.0.113.9"})
        honest = _Req("10.0.0.1", {"x-forwarded-for": "203.0.113.9"})
        assert _client_ip(spoofed) == _client_ip(honest), (
            "a caller-chosen prefix changed the bucket, which is the bypass"
        )

    def test_no_header_falls_back_to_the_peer(self):
        assert _client_ip(_Req("203.0.113.9", {})) == "203.0.113.9"

    def test_a_malformed_value_falls_back_to_the_peer(self):
        req = _Req("203.0.113.9", {"x-forwarded-for": "not-an-ip"})
        assert _client_ip(req) == "203.0.113.9"

    def test_missing_client_is_survivable(self):
        assert _client_ip(_Req(None, {})) == "unknown"
