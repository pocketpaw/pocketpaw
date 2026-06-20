# tests/cloud/test_w4a_migration.py — NEW (2026-06-10).
# Reproduces a migration bug found by a LIVE smoke of the sovereignty-waves
# integration branch: a fabric.db / instinct.db created BEFORE W4a's
# workspace_id column existed crashed on open with
# `sqlite3.OperationalError: no such column: workspace_id`. Root cause:
# SCHEMA_SQL's `CREATE INDEX ... ON <table>(workspace_id)` ran inside
# `executescript` BEFORE the ALTER TABLE ADD COLUMN migration, so on a
# pre-existing table (CREATE TABLE IF NOT EXISTS is a no-op) the index
# creation referenced a column that didn't exist yet. Unit tests only used
# fresh DBs, so they never hit the migration path — every existing deployment
# upgrading to W4a would. Fix: tenancy indexes are created after the ALTER.
#
# These tests build the REAL current schema, then strip just the workspace_id
# column (the faithful pre-W4a state — every other column present), and assert
# the store opens + reads + (for instinct) still verifies its audit chain.
import re
import sqlite3

import pytest

from pocketpaw.fabric.store import SCHEMA_SQL as FABRIC_SCHEMA
from pocketpaw.fabric.store import FabricStore
from pocketpaw.instinct.store import SCHEMA_SQL as INSTINCT_SCHEMA
from pocketpaw.instinct.store import InstinctStore


def _strip_workspace_id(schema_sql: str) -> str:
    """Return `schema_sql` with every `workspace_id TEXT` column declaration
    removed — reconstructing the exact pre-W4a CREATE TABLE text. Handles both
    a mid-list column (trailing comma) and a last column (preceding comma).
    More faithful than ALTER ... DROP COLUMN, which SQLite can't always rewrite
    (instinct_audit fails with 'incomplete input')."""
    schema_sql = re.sub(r"\n[ \t]*workspace_id TEXT,", "", schema_sql)  # mid-list
    schema_sql = re.sub(r",\n[ \t]*workspace_id TEXT\n", "\n", schema_sql)  # last col
    return schema_sql


def _make_pre_w4a_db(path, schema_sql: str) -> None:
    """Materialize a DB from the schema with workspace_id stripped, so it looks
    exactly like one written before the W4a column landed."""
    conn = sqlite3.connect(path)
    conn.executescript(_strip_workspace_id(schema_sql))
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_fabric_opens_pre_w4a_db(tmp_path):
    db = tmp_path / "fabric.db"
    _make_pre_w4a_db(db, FABRIC_SCHEMA)
    store = FabricStore(db)
    # Pre-fix this raised OperationalError: no such column: workspace_id
    assert await store.list_types() == []


@pytest.mark.asyncio
async def test_instinct_opens_pre_w4a_db(tmp_path):
    db = tmp_path / "instinct.db"
    _make_pre_w4a_db(db, INSTINCT_SCHEMA)
    store = InstinctStore(db)
    # Pre-fix this raised OperationalError: no such column: workspace_id
    assert await store.pending() == []
    # The W2b chain must still verify on a freshly-migrated legacy ledger.
    verdict = await store.verify_audit_chain()
    assert verdict["intact"] is True
