"""Gates on the short-TTL cache in front of the argon2 API-key verify.

The cache trades a small window of staleness for not re-deriving a 30ms hash
on every call, so what needs guarding is not that it caches - it is everything
it must still refuse while it does. Each test below names the mutation in
``tests/mutations/partials.json`` that breaks it.

Clocks are injected by swapping ``api_keys.time`` and ``api_keys.datetime``,
which are module attributes of that module alone. The TTL is monotonic and the
expiry is wall time, and the whole point of several of these tests is that
moving one does not move the other.
"""

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud.auth import api_keys as api_keys_service

_WORKSPACE = "ws_cache_1"
_OWNER = "u_cache_1"


class _FakeTime:
    """Stands in for the ``time`` module inside api_keys."""

    def __init__(self) -> None:
        self._now = 1000.0

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakeDatetime:
    """Stands in for the ``datetime`` CLASS inside api_keys.

    Returns real datetimes so every comparison downstream is a real one.
    """

    _offset = timedelta(0)

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001
        return datetime.now(tz or UTC) + cls._offset


class _StubDoc:
    """Minimal stand-in for an APIKey doc, for the pure-cache unit tests."""

    def __init__(self, key_id: str) -> None:
        self.id = key_id
        self.owner_user_id = _OWNER
        self.workspace = _WORKSPACE
        self.scopes = ["chat.send"]
        self.expires_at: datetime | None = None


@pytest_asyncio.fixture
async def clocks(mongo_db, monkeypatch):  # noqa: ARG001
    api_keys_service._reset_caches_for_tests()
    monkeypatch.delenv("POCKETPAW_API_KEY_VERIFY_TTL_SECONDS", raising=False)
    fake_time = _FakeTime()

    class _Dt(_FakeDatetime):
        _offset = timedelta(0)

    monkeypatch.setattr(api_keys_service, "time", fake_time)
    monkeypatch.setattr(api_keys_service, "datetime", _Dt)
    yield fake_time, _Dt
    api_keys_service._reset_caches_for_tests()


def _count_verifies(monkeypatch) -> list[int]:
    """Wrap the argon2 verify so tests can count derivations."""
    calls = [0]
    real = api_keys_service._password_hash.verify

    def _counting(secret, hashed):  # noqa: ANN001
        calls[0] += 1
        return real(secret, hashed)

    monkeypatch.setattr(api_keys_service._password_hash, "verify", _counting)
    return calls


async def _mint(*, expires_at: datetime | None = None, scopes: list[str] | None = None):
    doc, token = await api_keys_service.create_api_key(
        workspace_id=_WORKSPACE,
        owner_user_id=_OWNER,
        name="k",
        scopes=scopes if scopes is not None else ["chat.send"],
        expires_at=expires_at,
    )
    return doc, token


async def test_a_repeat_resolve_does_not_re_derive_argon2(clocks, monkeypatch):
    """Mutation: drop the cache lookup from resolve_bearer."""
    _doc, token = await _mint()
    calls = _count_verifies(monkeypatch)

    first = await api_keys_service.resolve_bearer(token)
    second = await api_keys_service.resolve_bearer(token)

    assert first == second == (_OWNER, _WORKSPACE, ["chat.send"])
    assert calls[0] == 1, "the second resolve re-derived the hash"


async def test_the_cache_expires_and_the_hash_is_derived_again(clocks, monkeypatch):
    """Mutation: never expire the entry (drop the TTL comparison)."""
    fake_time, _ = clocks
    _doc, token = await _mint()
    calls = _count_verifies(monkeypatch)

    await api_keys_service.resolve_bearer(token)
    fake_time.advance(api_keys_service._DEFAULT_VERIFY_TTL_SECONDS + 1.0)
    assert await api_keys_service.resolve_bearer(token) is not None

    assert calls[0] == 2, "a stale entry was served past its TTL"


async def test_a_zero_ttl_turns_the_cache_off(clocks, monkeypatch):
    """Mutation: ignore the 0 setting and cache anyway."""
    monkeypatch.setenv("POCKETPAW_API_KEY_VERIFY_TTL_SECONDS", "0")
    api_keys_service._reset_caches_for_tests()
    _doc, token = await _mint()
    calls = _count_verifies(monkeypatch)

    await api_keys_service.resolve_bearer(token)
    await api_keys_service.resolve_bearer(token)
    await api_keys_service.resolve_bearer(token)

    assert calls[0] == 3
    assert not api_keys_service._verify_cache


async def test_revoking_a_key_stops_it_working_immediately(clocks):
    """Mutation: drop the eviction from revoke_api_key.

    This is the finding's whole risk. Without the eviction the revoked key
    keeps authenticating for the rest of the TTL, and the cheap expiry check
    below it does not save you: the key has no expiry, only a revoked flag,
    and a cache hit never re-reads the flag.
    """
    doc, token = await _mint()
    assert await api_keys_service.resolve_bearer(token) is not None

    await api_keys_service.revoke_api_key(str(doc.id), _WORKSPACE)

    assert await api_keys_service.resolve_bearer(token) is None


async def test_the_member_removal_cascade_also_evicts(clocks):
    """Mutation: drop the eviction from revoke_keys_for_user_in_workspace."""
    _doc, token = await _mint()
    assert await api_keys_service.resolve_bearer(token) is not None

    flipped = await api_keys_service.revoke_keys_for_user_in_workspace(_OWNER, _WORKSPACE)

    assert flipped == 1
    assert await api_keys_service.resolve_bearer(token) is None


async def test_expiry_is_re_checked_on_every_hit_not_just_at_insert(clocks, monkeypatch):
    """Mutation: trust the entry once stored (drop the expiry re-check).

    The key is cached while in date, then time passes it. The TTL has NOT
    elapsed, so the entry is still there; only the wall clock moved. A cache
    that checked expiry once, at insert, authenticates an expired key here.

    The verify count is asserted too: it proves the refusal came from the
    cache path re-checking, not from a fresh database read doing the work.
    """
    fake_time, fake_dt = clocks
    _doc, token = await _mint(expires_at=datetime.now(UTC) + timedelta(seconds=5))
    calls = _count_verifies(monkeypatch)

    assert await api_keys_service.resolve_bearer(token) is not None
    assert calls[0] == 1

    fake_dt._offset = timedelta(seconds=30)  # past the key's expiry
    fake_time.advance(1.0)  # but well inside the 30s cache TTL

    assert await api_keys_service.resolve_bearer(token) is None
    assert calls[0] == 1, "it fell through to a fresh verify instead of refusing on the hit"


async def test_the_cache_is_bounded(clocks, monkeypatch):
    """Mutation: drop the LRU eviction loop.

    An unbounded map keyed on presented tokens is a memory leak any caller
    can drive, which is the same class of bug this PR's sibling fixes.
    """
    monkeypatch.setattr(api_keys_service, "_VERIFY_CACHE_MAX", 4)
    for index in range(12):
        api_keys_service._cache_verification(
            f"digest-{index}",
            _StubDoc(f"key-{index}"),
            now_monotonic=1000.0,
        )
    assert len(api_keys_service._verify_cache) == 4


async def test_scopes_handed_out_cannot_be_mutated_through_the_cache(clocks):
    """Mutation: hand back the stored scopes instead of a fresh list.

    Every caller receives the same object otherwise, and one of them appending
    to it rewrites what the next request is authorized to do. The type is
    asserted first because the entry stores a tuple: returning that directly
    would fail on the append rather than on the leak, which is a confusing
    way to learn about a privilege escalation.
    """
    _doc, token = await _mint(scopes=["chat.send", "files.read"])

    first = await api_keys_service.resolve_bearer(token)
    assert first is not None
    assert isinstance(first[2], list)
    first[2].append("admin.everything")

    second = await api_keys_service.resolve_bearer(token)
    assert second is not None
    assert second[2] == ["chat.send", "files.read"]


async def test_minting_a_key_hashes_off_the_event_loop(clocks, monkeypatch):
    """Mutation: call generate_key directly instead of generate_key_async.

    argon2's hash costs the same blocking 30ms as its verify. Asserting the
    thread identity is what distinguishes "ran in a worker" from "ran inline
    and happened to be fast".
    """
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = api_keys_service._password_hash.hash

    def _recording(secret):  # noqa: ANN001
        seen.append(threading.get_ident())
        return real(secret)

    monkeypatch.setattr(api_keys_service._password_hash, "hash", _recording)

    await _mint()

    assert seen and all(tid != loop_thread for tid in seen)


async def test_generate_key_async_leaves_the_loop_free(clocks):
    """The other direction: the loop keeps running while the hash derives."""
    ticks = 0

    async def _tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    ticker = asyncio.create_task(_tick())
    try:
        await api_keys_service.generate_key_async()
    finally:
        ticker.cancel()
    assert ticks > 0


@pytest.mark.parametrize("naive", [True, False])
async def test_a_naive_expiry_is_still_compared_in_utc(clocks, naive):
    """Mutation: compare expires_at without normalizing the tzinfo.

    Mongo hands datetimes back naive. Comparing a naive to an aware datetime
    raises TypeError, which resolve_bearer would surface as a 500 rather than
    a refusal - so the normalization is load-bearing on the real read path.
    """
    expired = datetime.now(UTC) - timedelta(hours=1)
    doc = _StubDoc("k1")
    doc.expires_at = expired.replace(tzinfo=None) if naive else expired
    api_keys_service._cache_verification("digest-x", doc, now_monotonic=1000.0)

    got = api_keys_service._cached_verification("digest-x", 1000.0, datetime.now(UTC))

    assert got is None
