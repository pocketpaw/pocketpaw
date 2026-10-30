# tests/test_paw_bar_store_wal.py — PawBarStore runs its SQLite file in WAL
# mode, so several server processes can share one paw_bar.db: a reader is not
# blocked while another connection holds the write lock.

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from pocketpaw.paw_bar.models import PawBarEvent
from pocketpaw.paw_bar.store import PawBarStore


@pytest.mark.asyncio
async def test_a_fresh_store_puts_its_file_in_wal_mode(tmp_path: Path):
    db = tmp_path / "paw_bar.db"
    store = PawBarStore(db)
    await store.recent_events("w1")

    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_a_read_is_not_blocked_by_another_connections_write_lock(tmp_path: Path):
    db = tmp_path / "paw_bar.db"
    store = PawBarStore(db)
    await store.record_event(PawBarEvent(widget_id="w1", type="t", customer_ref="r-00000001"))

    # Another process holds the write lock mid-transaction. Under the default
    # rollback journal, EXCLUSIVE locks every reader out until the busy timeout.
    writer = sqlite3.connect(db, isolation_level=None)
    try:
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute(
            "INSERT INTO paw_bar_events (widget_id, type, payload, customer_ref, timestamp)"
            " VALUES ('w1', 't', '{}', 'r-00000002', '2026-01-01T00:00:00')"
        )
        started = time.monotonic()
        events = await store.recent_events("w1")
        elapsed = time.monotonic() - started
        writer.execute("ROLLBACK")
    finally:
        writer.close()

    assert len(events) == 1  # the uncommitted row is invisible
    assert elapsed < 1.0
