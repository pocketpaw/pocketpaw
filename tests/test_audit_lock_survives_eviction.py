# tests/test_audit_lock_survives_eviction.py
# Created: 2026-06-26 (ISO-4 — audit-lock keyed outside the evictable store instance).
#
# Proves the ISO-4 fix for the chain-fork window the ISO-2 security review
# flagged: the Instinct audit-append lock is keyed by db_path in a process-global
# registry (pocketpaw._store_locks), NOT stored on the evictable store instance.
# So when the bounded-LRU store factory evicts a workspace's InstinctStore and
# builds a fresh one for the same file, BOTH instances share ONE lock and their
# concurrent appends still serialize — the chain cannot fork.
#
# Covers:
#   * two InstinctStore instances for the same file share the same lock object;
#   * different files get different locks (tenants never contend);
#   * the lock survives a real factory eviction (cap-1 LRU): a store fetched,
#     evicted, and re-fetched yields a handle whose lock matches the original's;
#   * end-to-end: interleaved appends across an eviction boundary keep the chain
#     intact (verify_audit_chain stays intact=True, no fork).

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import pocketpaw.stores as stores
from pocketpaw._store_locks import audit_lock_for, reset_audit_locks
from pocketpaw.instinct.models import ActionTrigger
from pocketpaw.instinct.store import InstinctStore

WS = "ws-lock"


def _trig() -> ActionTrigger:
    return ActionTrigger(type="agent", source="claude", reason="iso-4 lock test")


async def _drain_pending_tasks() -> None:
    """Run fire-and-forget eviction tasks (aclose) to completion before the test
    loop closes, so the aiosqlite worker doesn't race teardown."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path)
    monkeypatch.delenv("POCKETPAW_REQUIRE_WORKSPACE_SCOPE", raising=False)
    stores.reset_store_caches()
    reset_audit_locks()
    token = stores.current_workspace.set(None)
    try:
        yield
    finally:
        try:
            stores.current_workspace.reset(token)
        except ValueError:
            stores.current_workspace.set(None)
        stores.reset_store_caches()
        reset_audit_locks()


@pytest.mark.asyncio
async def test_same_file_two_instances_share_one_lock(tmp_path: Path) -> None:
    db = str(tmp_path / "instinct.db")
    a = InstinctStore(db)
    b = InstinctStore(db)
    # Distinct instances, SAME lock — the eviction-hole fix.
    assert a is not b
    assert a._log_lock is b._log_lock
    # And it's the registry's lock for that path.
    assert a._log_lock is audit_lock_for(db)


@pytest.mark.asyncio
async def test_different_files_get_different_locks(tmp_path: Path) -> None:
    a = InstinctStore(str(tmp_path / "a.db"))
    b = InstinctStore(str(tmp_path / "b.db"))
    assert a._log_lock is not b._log_lock  # tenants never contend


@pytest.mark.asyncio
async def test_lock_survives_a_real_factory_eviction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Evict a workspace's store via the cap-1 LRU; the rebuilt one keeps the lock."""
    monkeypatch.setattr(stores, "_WORKSPACE_STORE_CACHE_CAP", 1)
    stores.reset_store_caches()

    first = stores.get_instinct_store(workspace_id=WS)
    lock_before = first._log_lock

    # A second distinct workspace evicts WS (cap is 1).
    stores.get_instinct_store(workspace_id="ws-other")

    # Re-fetch WS → a NEW instance (it was evicted), but the SAME db file.
    rebuilt = stores.get_instinct_store(workspace_id=WS)
    assert rebuilt is not first  # genuinely evicted + rebuilt
    assert rebuilt._db_path == first._db_path
    # The audit lock is the SAME object across the eviction boundary — proof the
    # fork window is closed: an in-flight append on `first` and a new append on
    # `rebuilt` would contend on one lock.
    assert rebuilt._log_lock is lock_before
    await _drain_pending_tasks()


@pytest.mark.asyncio
async def test_chain_stays_intact_across_eviction_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent appends straddling an eviction keep the chain intact (no fork)."""
    monkeypatch.setattr(stores, "_WORKSPACE_STORE_CACHE_CAP", 1)
    stores.reset_store_caches()

    first = stores.get_instinct_store(workspace_id=WS)
    # Seed one action on the original instance.
    a0 = await first.propose("p", "a0", "", "", _trig())
    await first.approve(a0.id)

    # Evict WS, then re-fetch a fresh instance for the same file.
    stores.get_instinct_store(workspace_id="ws-other")
    rebuilt = stores.get_instinct_store(workspace_id=WS)
    assert rebuilt is not first

    # Fire concurrent appends from BOTH the stale and the rebuilt instance at the
    # same file. With a per-instance lock these could interleave their prev_hash
    # reads and fork; with the db_path-keyed lock they serialize.
    async def append(store: InstinctStore, n: int) -> None:
        act = await store.propose("p", f"t{n}", "", "", _trig())
        await store.approve(act.id)

    await asyncio.gather(
        append(first, 1),
        append(rebuilt, 2),
        append(first, 3),
        append(rebuilt, 4),
    )

    verdict = await rebuilt.verify_audit_chain()
    assert verdict["intact"] is True, verdict
    assert verdict["broken_at"] is None
    assert verdict["hashed"] == verdict["checked"]
    assert verdict["hashed"] > 0
    await _drain_pending_tasks()
