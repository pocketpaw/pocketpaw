# src/pocketpaw/sqlite_migrations.py — one-shot data migrations for a SQLite file.
#
# ``run_once(db, name, fn)`` applies a named data migration at most once per
# database file and records it in that file's ``schema_migrations(name,
# applied_at)`` table. The marker check, the migration body and the marker
# insert share ONE ``BEGIN IMMEDIATE`` transaction, so two processes opening the
# same file cannot both run it, and a failure rolls the whole file back (never
# half-migrated, marker not written, retried on the next open).
#
# Stores call it from their schema setup, after the DDL. Schema (DDL) changes
# stay additive in each store's SCHEMA_SQL; this is only for rewriting rows.
#
# ``is_applied`` / ``mark_applied`` serve a migration that commits per row (one
# transaction per widget, say) and records its marker only once every row moved;
# such a migration must be idempotent row by row, since a partial run is retried.
#
# ``checkpoint_wal(path)`` folds a file's WAL back into it with stdlib sqlite3.
# Store ``aclose`` methods run it via ``asyncio.to_thread``, never through an
# aiosqlite connection: eviction fires ``aclose`` as a task the loop may close
# under, and an orphaned aiosqlite worker is a non-daemon thread that blocks
# interpreter exit.

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import closing
from datetime import UTC, datetime

import aiosqlite

logger = logging.getLogger(__name__)

SCHEMA_MIGRATIONS_SQL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    " name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
)


async def run_once(
    db: aiosqlite.Connection,
    name: str,
    fn: Callable[[aiosqlite.Connection], Awaitable[None]],
) -> bool:
    """Run ``fn(db)`` inside one transaction unless ``name`` is already recorded.

    Returns True when the migration ran now. ``fn`` must not commit; it runs on
    ``db`` inside the open transaction. Any exception rolls back and propagates.
    """
    await db.execute(SCHEMA_MIGRATIONS_SQL)
    await db.commit()
    await db.execute("BEGIN IMMEDIATE")
    try:
        async with db.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (name,)) as cur:
            if await cur.fetchone():
                await db.rollback()
                return False
        await fn(db)
        await db.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
            (name, datetime.now(UTC).isoformat()),
        )
        await db.commit()
    except BaseException:
        await db.rollback()
        raise
    return True


async def is_applied(db: aiosqlite.Connection, name: str) -> bool:
    """Whether ``name`` is recorded in this file's ``schema_migrations``."""
    await db.execute(SCHEMA_MIGRATIONS_SQL)
    await db.commit()
    async with db.execute("SELECT 1 FROM schema_migrations WHERE name = ?", (name,)) as cur:
        return await cur.fetchone() is not None


async def mark_applied(db: aiosqlite.Connection, name: str) -> None:
    """Record ``name`` as applied (idempotent) and commit."""
    await db.execute(SCHEMA_MIGRATIONS_SQL)
    await db.execute(
        "INSERT OR IGNORE INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (name, datetime.now(UTC).isoformat()),
    )
    await db.commit()


def checkpoint_wal(db_path: str) -> None:
    """Truncate ``db_path``'s write-ahead log (blocking; run it off the loop)."""
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
