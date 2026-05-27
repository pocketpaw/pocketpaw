"""Tests for the per-(actor, workspace) invite-create rate limiter.

Exercises the `rate_limit_invite_create` Depends directly with synthetic
RequestContext values — keeps the test focused on the bucket behavior
without needing the full FastAPI route + Mongo fixture stack.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core import rate_limit as rate_limit_mod
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import RateLimited


def _ctx(user_id: str) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=None,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch: pytest.MonkeyPatch):
    """Reset the module-level bucket so tests don't leak state into each other."""
    from pocketpaw.security.rate_limiter import RateLimiter

    fresh = RateLimiter(rate=50.0 / 86400.0, capacity=50)
    monkeypatch.setattr(rate_limit_mod, "_invite_create_limiter", fresh)
    yield


async def test_invite_create_allows_up_to_capacity():
    ctx = _ctx("user-a")
    for _ in range(50):
        # No exception → allowed.
        await rate_limit_mod.rate_limit_invite_create("ws-1", ctx)


async def test_invite_create_blocks_on_51st_per_actor_workspace():
    ctx = _ctx("user-a")
    for _ in range(50):
        await rate_limit_mod.rate_limit_invite_create("ws-1", ctx)
    with pytest.raises(RateLimited) as excinfo:
        await rate_limit_mod.rate_limit_invite_create("ws-1", ctx)
    assert excinfo.value.status_code == 429
    assert excinfo.value.code == "workspace.invite_rate_limited"


async def test_bulk_consume_is_atomic_no_partial_burn():
    """A bulk request that overflows the bucket must consume zero tokens.

    The earlier loop-of-checks burnt as many tokens as it could before
    raising; ``consume_invite_create_tokens`` now uses ``try_consume`` so
    a failed bulk leaves the full budget for the next attempt.
    """
    # Bucket holds 50. Asking for 51 must fail without spending any.
    with pytest.raises(RateLimited):
        rate_limit_mod.consume_invite_create_tokens("user-a", "ws-1", 51)
    # Followed by a 50-batch that should succeed because nothing was burnt.
    rate_limit_mod.consume_invite_create_tokens("user-a", "ws-1", 50)
    # And the 51st single token still fails.
    with pytest.raises(RateLimited):
        rate_limit_mod.consume_invite_create_tokens("user-a", "ws-1", 1)


async def test_invite_create_buckets_per_actor_per_workspace():
    """Two workspaces and two actors each get their own bucket."""
    actor_a = _ctx("user-a")
    actor_b = _ctx("user-b")

    # Exhaust user-a on ws-1.
    for _ in range(50):
        await rate_limit_mod.rate_limit_invite_create("ws-1", actor_a)
    with pytest.raises(RateLimited):
        await rate_limit_mod.rate_limit_invite_create("ws-1", actor_a)

    # user-a on a different workspace still has a full bucket.
    await rate_limit_mod.rate_limit_invite_create("ws-2", actor_a)

    # user-b on ws-1 still has a full bucket.
    await rate_limit_mod.rate_limit_invite_create("ws-1", actor_b)
