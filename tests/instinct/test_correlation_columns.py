# tests/instinct/test_correlation_columns.py
# Created: 2026-08-05 (T-3, coupling-gap wave) — coverage for the ADDITIVE
# Decision-Graph chain-id columns on the Instinct store. Until T-3 the chain ids
# lived only inside the per-kind untyped ``parameters`` blobs and were
# back-written by best-effort raw SQL after propose, so a failed back-write left
# an action permanently unjoinable against its Decision chain. These are OSS
# store tests — no cloud extras, no beanie, just a tmp SQLite db. Pins that:
#   1. propose(correlation_id=..., proposed_event_id=...) populates the columns
#      and they round-trip through get_action / list_actions / pending.
#   2. A plain OSS propose (no ids) stores NULLs and nothing breaks — the
#      pre-T-3 behaviour, unchanged.
#   3. set_chain_ids writes each column independently, is status-preserving,
#      never blanks an already-stored id, and returns None for an unknown id.
#   4. A pre-T-3 DB file (columns stripped from the schema) upgrades cleanly on
#      open and its old rows read as NULL.
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from pocketpaw.instinct.models import ActionStatus, ActionTrigger
from pocketpaw.instinct.store import SCHEMA_SQL, InstinctStore

pytestmark = pytest.mark.asyncio

TRIGGER = ActionTrigger(type="agent", source="claude", reason="chain-id test")

CORR = "11111111-2222-3333-4444-555555555555"
EVENT = "99999999-8888-7777-6666-555555555555"


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "chain.db")


async def test_propose_with_chain_ids_populates_columns(store: InstinctStore) -> None:
    """propose with the chain ids stores them as COLUMNS, readable back on the
    Action without touching ``parameters``."""
    action = await store.propose(
        pocket_id="w1",
        title="gated call",
        description="",
        recommendation="",
        trigger=TRIGGER,
        correlation_id=CORR,
        proposed_event_id=EVENT,
    )
    assert action.correlation_id == CORR
    assert action.proposed_event_id == EVENT

    fetched = await store.get_action(action.id)
    assert fetched is not None
    assert fetched.correlation_id == CORR
    assert fetched.proposed_event_id == EVENT

    # And the ids survive the list / pending read paths too (same row mapper).
    listed = await store.list_actions(pocket_id="w1")
    assert [r.correlation_id for r in listed] == [CORR]
    pending = await store.pending(pocket_id="w1")
    assert [r.proposed_event_id for r in pending] == [EVENT]


async def test_propose_with_chain_ids_keeps_blob_copies(store: InstinctStore) -> None:
    """The columns are ADDITIVE — a caller that also carries the ids inside its
    per-kind blob keeps that copy verbatim (blob-schema compat)."""
    blob = {"kind": "external_action", "schema": 1, "correlation_id": CORR}
    action = await store.propose(
        pocket_id="w1",
        title="gated call",
        description="",
        recommendation="",
        trigger=TRIGGER,
        parameters={"_external_action": blob},
        correlation_id=CORR,
    )
    fetched = await store.get_action(action.id)
    assert fetched is not None
    assert fetched.correlation_id == CORR
    assert fetched.parameters["_external_action"]["correlation_id"] == CORR
    assert fetched.parameters["_external_action"]["schema"] == 1


async def test_propose_without_chain_ids_stores_nulls(store: InstinctStore) -> None:
    """The plain OSS propose path is untouched: no chain ids given → both
    columns NULL, every read still works."""
    action = await store.propose(
        pocket_id="pocket-1",
        title="ordinary proposal",
        description="",
        recommendation="",
        trigger=TRIGGER,
    )
    assert action.correlation_id is None
    assert action.proposed_event_id is None

    fetched = await store.get_action(action.id)
    assert fetched is not None
    assert fetched.correlation_id is None
    assert fetched.proposed_event_id is None

    # The lifecycle still runs end-to-end on a chain-less action.
    approved = await store.approve(action.id, approver="u1")
    assert approved is not None
    assert approved.status == ActionStatus.APPROVED
    assert approved.correlation_id is None

    # And the raw column really is NULL, not the string "None".
    with sqlite3.connect(store._db_path) as conn:
        row = conn.execute(
            "SELECT correlation_id, proposed_event_id FROM instinct_actions WHERE id = ?",
            (action.id,),
        ).fetchone()
    assert row == (None, None)


async def test_set_chain_ids_fills_event_id_without_touching_status(
    store: InstinctStore,
) -> None:
    """The late-arriving ``agent.proposed`` event id lands via set_chain_ids —
    a status-preserving column write that leaves the correlation intact."""
    action = await store.propose(
        pocket_id="w1",
        title="gated call",
        description="",
        recommendation="",
        trigger=TRIGGER,
        correlation_id=CORR,
    )
    assert action.proposed_event_id is None

    updated = await store.set_chain_ids(action.id, proposed_event_id=EVENT)
    assert updated is not None
    # The correlation that landed at INSERT is NOT blanked by a partial write.
    assert updated.correlation_id == CORR
    assert updated.proposed_event_id == EVENT
    # Status-preserving — this is not a lifecycle write.
    assert updated.status == ActionStatus.PENDING


async def test_set_chain_ids_unknown_action_and_empty_call(store: InstinctStore) -> None:
    """An id that doesn't resolve returns None; so does a call with nothing to
    write (no accidental ``updated_at``-only churn)."""
    assert await store.set_chain_ids("act_missing", correlation_id=CORR) is None

    action = await store.propose(
        pocket_id="w1",
        title="gated call",
        description="",
        recommendation="",
        trigger=TRIGGER,
    )
    assert await store.set_chain_ids(action.id) is None


def _strip_chain_columns(schema_sql: str) -> str:
    """Return ``schema_sql`` with the T-3 chain-id column declarations removed —
    reconstructing the pre-T-3 CREATE TABLE text. Same technique as
    tests/cloud/test_w4a_migration.py's ``_strip_workspace_id`` (an ALTER ...
    DROP COLUMN can't always be rewritten by SQLite)."""
    for col in ("correlation_id", "proposed_event_id"):
        schema_sql = re.sub(rf"\n[ \t]*{col} TEXT,", "", schema_sql)
    return schema_sql


async def test_pre_t3_db_upgrades_cleanly_and_old_rows_read_null(tmp_path: Path) -> None:
    """A DB file written BEFORE the chain-id columns existed opens cleanly (the
    guarded ALTERs run), its pre-existing rows read as NULL, and a fresh propose
    on the migrated file can carry the ids."""
    db = tmp_path / "instinct_pre_t3.db"
    pre_t3_schema = _strip_chain_columns(SCHEMA_SQL)
    assert "correlation_id" not in pre_t3_schema  # the fixture really is pre-T-3

    conn = sqlite3.connect(db)
    conn.executescript(pre_t3_schema)
    # A legacy row written by the old code — no chain-id columns to write to.
    conn.execute(
        "INSERT INTO instinct_actions (id, pocket_id, title, trigger, parameters)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            "act_legacy",
            "pocket-old",
            "legacy proposal",
            TRIGGER.model_dump_json(),
            '{"_external_action": {"correlation_id": "blob-only-id"}}',
        ),
    )
    conn.commit()
    conn.close()

    store = InstinctStore(db)
    # Pre-fix this raised OperationalError: no such column: correlation_id.
    legacy = await store.get_action("act_legacy")
    assert legacy is not None
    assert legacy.correlation_id is None
    assert legacy.proposed_event_id is None
    # The old blob copy is untouched — nothing was backfilled or guessed.
    assert legacy.parameters["_external_action"]["correlation_id"] == "blob-only-id"

    # The migrated file takes new chain-carrying rows.
    fresh = await store.propose(
        pocket_id="pocket-old",
        title="post-migration",
        description="",
        recommendation="",
        trigger=TRIGGER,
        correlation_id=CORR,
    )
    assert (await store.get_action(fresh.id)).correlation_id == CORR
    # Both rows co-exist on the legacy read path.
    assert len(await store.list_actions(pocket_id="pocket-old")) == 2
