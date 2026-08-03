"""Shared single-use OAuth state store (AM-1).

These pin the properties that make ``state`` worth having at all. The reference
failure is CVE-2025-68481 / GHSA-5j53-63w8-8625 against fastapi-users, where
OAuth state was a stateless JWT carrying only an audience and an expiry: it
verified for anyone holding it, so an attacker could start a flow, finish the
upstream consent with their own account, and hand the resulting
``?code=…&state=…`` to a victim. Single-use server-side state is what makes
that replay impossible, so "consume twice" is the most important test here.

Uses fakeredis, matching tests/cloud/auth/test_sso.py, so TTL and delete
semantics are the real ones rather than a hand-rolled dict.
"""

import os

os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.auth import _oauth_state


@pytest.fixture(autouse=True)
def _fake_redis(monkeypatch):
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    return fake


# ---------------------------------------------------------------------------
# Issue / consume round trip
# ---------------------------------------------------------------------------


async def test_round_trip_returns_the_payload():
    state = await _oauth_state.issue("social", {"provider": "google", "flow": "web"})
    assert await _oauth_state.consume("social", state) == {
        "provider": "google",
        "flow": "web",
    }


async def test_state_is_unguessable_and_unique():
    a = await _oauth_state.issue("social", {"n": 1})
    b = await _oauth_state.issue("social", {"n": 2})
    assert a != b
    # 32 random bytes, urlsafe-base64 -> comfortably over 40 chars.
    assert len(a) >= 40


async def test_payload_is_not_recoverable_from_the_state_value():
    # The state is a reference, not a container: nothing about the payload may
    # be derivable from the value handed to the browser.
    state = await _oauth_state.issue("social", {"provider": "github"})
    assert "github" not in state


# ---------------------------------------------------------------------------
# Single use — the replay defence
# ---------------------------------------------------------------------------


async def test_consuming_twice_is_refused():
    state = await _oauth_state.issue("social", {"provider": "google"})
    await _oauth_state.consume("social", state)

    with pytest.raises(Forbidden) as exc:
        await _oauth_state.consume("social", state)
    assert exc.value.code == "social.invalid_state"


async def test_consume_deletes_even_when_the_caller_never_looks_again(_fake_redis):
    state = await _oauth_state.issue("social", {"provider": "google"})
    await _oauth_state.consume("social", state)
    assert await _fake_redis.get(f"social_state:{state}") is None


async def test_unknown_state_is_refused():
    with pytest.raises(Forbidden) as exc:
        await _oauth_state.consume("social", "never-issued")
    assert exc.value.code == "social.invalid_state"


# ---------------------------------------------------------------------------
# Namespacing — one flow cannot spend another's state
# ---------------------------------------------------------------------------


async def test_a_state_issued_for_sso_cannot_be_spent_on_social():
    state = await _oauth_state.issue("sso", {"workspace_id": "w1"})

    with pytest.raises(Forbidden) as exc:
        await _oauth_state.consume("social", state)
    assert exc.value.code == "social.invalid_state"

    # ...and is still redeemable by its own flow: the failed cross-flow attempt
    # must not have consumed it.
    assert await _oauth_state.consume("sso", state) == {"workspace_id": "w1"}


async def test_namespace_drives_the_error_code():
    with pytest.raises(Forbidden) as exc:
        await _oauth_state.consume("sso", "nope")
    assert exc.value.code == "sso.invalid_state"


async def test_sso_namespace_keeps_its_historical_redis_key(_fake_redis):
    # SSO states in flight across a deploy must still redeem, so the key shape
    # is part of the contract, not an implementation detail.
    state = await _oauth_state.issue("sso", {"workspace_id": "w1"})
    assert await _fake_redis.get(f"sso_state:{state}") is not None


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


async def test_state_carries_a_ttl(_fake_redis):
    state = await _oauth_state.issue("social", {"provider": "google"})
    ttl = await _fake_redis.ttl(f"social_state:{state}")
    assert 0 < ttl <= _oauth_state.STATE_TTL_SECONDS


async def test_ttl_is_overridable():
    state = await _oauth_state.issue("social", {"a": 1}, ttl_seconds=60)
    assert await _oauth_state.consume("social", state) == {"a": 1}


async def test_expired_state_is_refused(_fake_redis):
    state = await _oauth_state.issue("social", {"provider": "google"})
    # Simulate the TTL elapsing rather than sleeping through it.
    await _fake_redis.delete(f"social_state:{state}")

    with pytest.raises(Forbidden):
        await _oauth_state.consume("social", state)


# ---------------------------------------------------------------------------
# Malformed payloads
# ---------------------------------------------------------------------------


async def test_malformed_payload_is_refused_not_returned(_fake_redis):
    await _fake_redis.setex("social_state:corrupt", 600, "{not json")

    with pytest.raises(Forbidden) as exc:
        await _oauth_state.consume("social", "corrupt")
    assert exc.value.code == "social.invalid_state"


async def test_non_object_payload_is_refused(_fake_redis):
    # A bare JSON scalar parses fine but is not a payload; callers index into
    # the result, so returning it would raise somewhere far less obvious.
    await _fake_redis.setex("social_state:scalar", 600, '"just-a-string"')

    with pytest.raises(Forbidden) as exc:
        await _oauth_state.consume("social", "scalar")
    assert exc.value.code == "social.invalid_state"


# ---------------------------------------------------------------------------
# PKCE
# ---------------------------------------------------------------------------


def test_pkce_pair_is_s256_of_the_verifier():
    import base64
    import hashlib

    verifier, challenge = _oauth_state.pkce_pair()
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    assert challenge == expected


def test_pkce_pair_is_unpadded_and_fresh_each_call():
    v1, c1 = _oauth_state.pkce_pair()
    v2, _ = _oauth_state.pkce_pair()
    assert v1 != v2
    assert "=" not in v1 and "=" not in c1


def test_nonce_is_fresh_each_call():
    assert _oauth_state.new_nonce() != _oauth_state.new_nonce()
