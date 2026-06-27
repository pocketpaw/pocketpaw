# src/pocketpaw/_store_locks.py
# Created: 2026-06-26 (ISO-4 — audit-lock carry from the ISO-2 security review).
#
# Process-global registry of per-db-file asyncio locks, keyed by db_path. The
# Instinct audit hash-chain is serialized by a lock held across the chain
# read-head + insert in InstinctStore._log: without it, two concurrent _log
# calls could both read the same prev_hash before either inserts, forking the
# chain. Until ISO-4 that lock lived on the STORE INSTANCE (self._log_lock).
#
# The ISO-2 security review flagged the gap that motivates this module: under
# the workspace-keyed factory's bounded LRU (ISO-1/2), a workspace's
# InstinctStore can be EVICTED and a NEW instance built for the same file while
# an async _log on the old instance is still in flight. Two instances for the
# same db_path each held their OWN per-instance lock, so they did NOT mutually
# exclude — re-opening the within-tenant chain-fork window the lock exists to
# close. Keying the lock by db_path here (OUTSIDE any evictable store instance)
# means every instance that targets the same file shares ONE lock, so eviction
# can never split it. Different files (different tenants) still get different
# locks and never contend — the property the old per-instance design wanted,
# now without the eviction hole.

from __future__ import annotations

import asyncio

# (db_path, running-loop id) -> the lock guarding that file's audit-chain
# append. Module-global so it survives store eviction/recreation.
#
# Why the loop id is part of the key: an ``asyncio.Lock`` bound to one running
# loop cannot be awaited under a different loop (it raises "bound to a different
# event loop"). Production runs a single long-lived loop, so this degenerates to
# "one lock per db file" — exactly what we want. The pytest-asyncio suite runs a
# FRESH loop per test, so without the loop in the key a lock cached under test
# A's loop would blow up in test B. Scoping by loop gives each loop its own lock
# per file with zero introspection of private Lock internals, and never weakens
# isolation: a brand-new loop has no in-flight appends from the old one to
# serialize against, and within ANY single loop every instance for the same file
# still shares one lock (the eviction-hole fix).
_locks: dict[tuple[str, int], asyncio.Lock] = {}


def audit_lock_for(db_path: str) -> asyncio.Lock:
    """Return the process-global audit-append lock for ``db_path``.

    All InstinctStore instances pointing at the same file (under the same event
    loop) get the SAME lock, so a store evicted+rebuilt mid-append still
    serializes against its replacement. Must be called with a running event
    loop (it always is — it is invoked from inside an ``async`` append).
    """
    loop = asyncio.get_running_loop()
    key = (db_path, id(loop))
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def reset_audit_locks() -> None:
    """Drop every cached lock. For tests that need a clean registry."""
    _locks.clear()
