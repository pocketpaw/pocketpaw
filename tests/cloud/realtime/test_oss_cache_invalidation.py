"""The OSS settings and skills caches drop on every web process.

``pocketpaw.cache_invalidation`` (OSS) clears the local cache and calls a remote
hook; ``mount_cloud`` fills that hook with the realtime broadcast. Two
simulated processes share the real local Redis through their own broadcast
``Channel`` on a unique stream. What is pinned:

- a settings write on A clears A's cache at once and B's within a stream read;
- a skill install/remove on A reloads the loader on A and on B;
- B runs the local half only, so the drop is not relayed back around;
- the OSS hook itself: no remote without a hook, ``local_only`` skips it, and a
  failing hook never fails the write.

Mutations: ``tests/mutations/multiworker_safety.json``. The two-process tests
skip when no Redis answers on localhost:6379.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from pocketpaw_ee.cloud._core.realtime import broadcast
from pocketpaw_ee.cloud._core.realtime.broadcast import Channel

from pocketpaw import cache_invalidation
from pocketpaw.config import get_settings

pytestmark = pytest.mark.asyncio

BLOCK_MS = 100


@pytest_asyncio.fixture
async def redis():
    client = aioredis.Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        pytest.skip("no Redis on localhost:6379")
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
def _reset():
    broadcast._reset_for_tests()
    cache_invalidation.set_remote_invalidator(None)
    yield
    broadcast._reset_for_tests()
    cache_invalidation.set_remote_invalidator(None)


class _Recorder:
    """B's invalidator table: the real registered functions, counted."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def table(self) -> dict:
        def wrap(name, fn):
            def run(key):
                self.calls.append(name)
                return fn(key)

            return run

        return {name: wrap(name, fn) for name, fn in broadcast._INVALIDATORS.items()}


@pytest_asyncio.fixture
async def two_procs(redis):
    """A is this process's active channel (what the OSS hook publishes on);
    B is a sibling reading the same stream with the real invalidators."""
    from pocketpaw_ee.cloud import _wire_oss_cache_invalidation

    stream = f"test:realtime:broadcast:{uuid.uuid4().hex}"
    _wire_oss_cache_invalidation()
    broadcast.configure(enabled=True)
    chan_a = Channel(redis, stream=stream, block_ms=BLOCK_MS)
    broadcast._channel = chan_a
    recorder = _Recorder()
    chan_b = Channel(redis, stream=stream, block_ms=BLOCK_MS, invalidators=recorder.table())
    await chan_a.start()
    await chan_b.start()
    yield recorder
    await chan_a.stop()
    await chan_b.stop()
    await redis.delete(stream)


async def _until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out waiting for predicate")


async def test_settings_write_on_a_clears_b(two_procs):
    recorder = two_procs
    get_settings()
    cache_invalidation.clear_settings_cache()  # the write path on A
    after_a = get_settings()  # A re-caches

    await _until(lambda: cache_invalidation.SETTINGS in recorder.calls)
    await asyncio.sleep(BLOCK_MS / 1000 * 4)  # a relayed-back drop would land here

    assert get_settings() is not after_a  # B's run cleared the real cache
    assert recorder.calls == [cache_invalidation.SETTINGS]


async def test_skill_install_on_a_reloads_b(two_procs, monkeypatch):
    recorder = two_procs

    class _Loader:
        reloads = 0

        def reload(self):
            _Loader.reloads += 1
            return {}

    loader = _Loader()
    monkeypatch.setattr("pocketpaw.skills.get_skill_loader", lambda: loader)

    cache_invalidation.reload_skills()  # the install path on A
    assert _Loader.reloads == 1

    await _until(lambda: _Loader.reloads == 2)
    await asyncio.sleep(BLOCK_MS / 1000 * 4)

    assert _Loader.reloads == 2
    assert recorder.calls == [cache_invalidation.SKILLS]


async def test_installer_uses_the_cross_process_reload():
    """install_skill_from_source reloads through the hook, not the bare loader."""
    import inspect

    from pocketpaw.skills import installer

    assert installer.reload_skills is cache_invalidation.reload_skills
    assert "reload_skills()" in inspect.getsource(installer.install_skill_from_source)


# --- the OSS hook on its own -----------------------------------------------------


async def test_no_remote_hook_means_local_only():
    get_settings()
    before = get_settings()
    cache_invalidation.clear_settings_cache()
    assert get_settings() is not before


async def test_remote_hook_gets_the_cache_name_unless_local_only():
    seen: list[str] = []
    cache_invalidation.set_remote_invalidator(seen.append)

    cache_invalidation.clear_settings_cache()
    cache_invalidation.clear_settings_cache(local_only=True)

    assert seen == [cache_invalidation.SETTINGS]


async def test_a_failing_remote_hook_never_fails_the_write():
    def boom(_name):
        raise RuntimeError("relay down")

    cache_invalidation.set_remote_invalidator(boom)
    before = get_settings()

    cache_invalidation.clear_settings_cache()

    assert get_settings() is not before
