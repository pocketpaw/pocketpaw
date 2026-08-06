# tests/instinct/test_audit_correlation.py
# Created: 2026-08-06 (T-4, coupling-gap wave) — coverage for the Decision-Graph
# ``correlation_id`` on the tamper-evident Instinct audit ledger. Before T-4 the
# ledger could prove "action X was approved by Y" but could not be joined to the
# Decision that explains WHY: the legal record and the explainability record were
# two disconnected chains over the same event. An audit row now copies the
# action's first-class ``correlation_id`` COLUMN (added by T-3).
#
# The load-bearing invariant is NEGATIVE: correlation_id must stay OUT of the
# hash material, exactly as ``workspace_id`` (W4a) does — otherwise every ledger
# written before this change reads as tampered. Pinned here:
#   1. a new audit row carries its action's correlation id, on BOTH append paths
#      (propose -> ``_log``, approve -> the in-transaction ``_update_status``);
#   2. an action with no chain produces a NULL audit correlation, no crash, and
#      an action-less ``log()`` row is fine too;
#   3. the canonical payload + its digest are byte-identical to pre-T-4 (a
#      hard-coded known-good string + two sha256s + a signature check), so a
#      refactor that folds the field into the hash fails loudly;
#   4. a pre-T-4 ledger of REAL chained rows still verifies after the guarded
#      ALTER, and keeps verifying once new rows are appended on top of it.
from __future__ import annotations

import inspect
import re
import sqlite3
from pathlib import Path

import pytest

from pocketpaw.instinct.models import ActionTrigger
from pocketpaw.instinct.store import (
    SCHEMA_SQL,
    InstinctStore,
    _canonical_audit_payload,
    compute_audit_hash,
)

TRIGGER = ActionTrigger(type="agent", source="claude", reason="audit chain-id test")

CORR = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture
def store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "audit_chain.db")


def _audit_rows(store: InstinctStore) -> list[tuple]:
    """Raw (id, correlation_id) straight out of SQLite, in insertion order —
    so a NULL is proven to be a real NULL and not the string "None"."""
    with sqlite3.connect(store._db_path) as conn:
        return conn.execute(
            "SELECT id, correlation_id FROM instinct_audit ORDER BY rowid"
        ).fetchall()


# ---------------------------------------------------------------------------
# 1. the join lands — on both append paths
# ---------------------------------------------------------------------------


async def test_audit_rows_carry_the_actions_correlation_id(store: InstinctStore) -> None:
    """A gated action's audit rows carry its chain key, so the legal record
    joins to the Decision that explains it.

    Covers BOTH writers: ``propose`` appends through ``_log`` (own connection,
    own commit) and ``approve`` through ``_update_status`` (inside the status
    UPDATE's transaction). They share ``_append_audit_locked``, which is where
    the id is read from the action's column — mutating that read to ``None``
    fails this test.
    """
    action = await store.propose(
        pocket_id="w1",
        title="gated call",
        description="",
        recommendation="",
        trigger=TRIGGER,
        correlation_id=CORR,
    )
    approved = await store.approve(action.id, approver="maya")
    assert approved is not None

    entries = {e.event: e for e in await store.query_audit(pocket_id="w1")}
    assert set(entries) == {"action_proposed", "action_approved"}
    assert entries["action_proposed"].correlation_id == CORR
    assert entries["action_approved"].correlation_id == CORR

    # The single-row read path (the Why? drawer's fetch) carries it too.
    one = await store.get_audit_entry(entries["action_approved"].id)
    assert one is not None
    assert one.correlation_id == CORR

    # It is a real stored column, not a model default.
    assert [corr for _id, corr in _audit_rows(store)] == [CORR, CORR]

    # And the tamper-evident chain is unaffected by the new column.
    verdict = await store.verify_audit_chain()
    assert verdict["intact"] is True
    assert verdict["hashed"] == 2


async def test_chainless_action_writes_null_and_never_crashes(store: InstinctStore) -> None:
    """An OSS proposal opens no Decision chain. Its audit rows must store a real
    NULL and the lifecycle must run end-to-end regardless — a missing
    explainability join can never cost anyone an approval."""
    action = await store.propose(
        pocket_id="pocket-1",
        title="ordinary proposal",
        description="",
        recommendation="",
        trigger=TRIGGER,
    )
    assert action.correlation_id is None
    approved = await store.approve(action.id, approver="maya")
    assert approved is not None

    entries = await store.query_audit(pocket_id="pocket-1")
    assert [e.correlation_id for e in entries] == [None, None]
    assert [corr for _id, corr in _audit_rows(store)] == [None, None]
    assert (await store.verify_audit_chain())["intact"] is True


async def test_action_less_audit_row_is_fine(store: InstinctStore) -> None:
    """``log()`` writes audit rows with no ``action_id`` at all (system events).
    The correlation lookup must short-circuit rather than query for NULL."""
    entry = await store.log(actor="system", event="store_opened", description="boot")
    assert entry.correlation_id is None
    assert [corr for _id, corr in _audit_rows(store)] == [None]
    assert (await store.verify_audit_chain())["intact"] is True


# ---------------------------------------------------------------------------
# 2. the hash material is frozen
# ---------------------------------------------------------------------------

# A known-good sample of the W2b hash material, captured from the code as it
# stood BEFORE T-4 added the column. These constants are the alarm: fold
# correlation_id (or anything else) into ``_canonical_audit_payload`` and the
# serialization changes, the digests change, and every ledger ever written stops
# verifying. Mutation-proven — see tests/mutations/audit_chain_correlation.json.
PINNED_ROW: dict = {
    "id": "aud_t4pin000000",
    "action_id": "act_t4pin000000",
    "pocket_id": "pocket-pin",
    "timestamp": "2026-08-06T00:00:00",
    "actor": "user:pin",
    "event": "action_approved",
    "category": "decision",
    "description": "Action Approved: pinned row",
    "context": {"b": 2, "a": 1},
    "ai_recommendation": None,
    "outcome": None,
}
PINNED_CANONICAL = (
    '{"action_id":"act_t4pin000000","actor":"user:pin","ai_recommendation":null,'
    '"category":"decision","context":"{\\"a\\":1,\\"b\\":2}",'
    '"description":"Action Approved: pinned row","event":"action_approved",'
    '"id":"aud_t4pin000000","outcome":null,"pocket_id":"pocket-pin",'
    '"timestamp":"2026-08-06T00:00:00"}'
)
PINNED_GENESIS_HASH = "7ebe4128ad861ac007d723a79b27d3b5dc073e8414d47f5c1af25db3bf3d6e84"
PINNED_LINKED_HASH = "c0217c21a270562ef792f337b20c72d1fa6e10b015bea1c3e3882a1b4b17688b"


def test_canonical_payload_and_digest_are_frozen() -> None:
    """The exact bytes an auditor's chain is built from. If this fails, the
    change under review invalidates every existing ledger — that is the whole
    signal, and it must never be "fixed" by re-pinning the constants."""
    canonical = _canonical_audit_payload(**PINNED_ROW)
    assert canonical == PINNED_CANONICAL
    assert "correlation_id" not in canonical
    assert compute_audit_hash(canonical, "") == PINNED_GENESIS_HASH
    assert compute_audit_hash(canonical, "deadbeef") == PINNED_LINKED_HASH


def test_join_columns_are_absent_from_the_hash_signature() -> None:
    """Structural half of the freeze: the canonical payload has no PARAMETER for
    the join columns, so folding one in cannot happen by accident at a call
    site. ``workspace_id`` has been excluded this way since W4a; T-4's
    ``correlation_id`` joins it on the same terms."""
    params = set(inspect.signature(_canonical_audit_payload).parameters)
    assert "workspace_id" not in params
    assert "correlation_id" not in params


# ---------------------------------------------------------------------------
# 3. old ledgers migrate and keep verifying
# ---------------------------------------------------------------------------


def _strip_audit_correlation(schema_sql: str) -> str:
    """Return ``schema_sql`` with the T-4 audit join column (and its comment)
    removed — reconstructing the pre-T-4 CREATE TABLE text. Same technique as
    tests/cloud/test_w4a_migration.py's ``_strip_workspace_id``: SQLite cannot
    always rewrite an ALTER ... DROP COLUMN, so the fixture is built from the
    real current schema with one column excised. The anchor is the table-final
    ``correlation_id TEXT\n);`` — ``instinct_actions``' own correlation column
    is mid-list (trailing comma) and is deliberately left in place, because a
    pre-T-4 DB is post-T-3."""
    return re.sub(
        r",\n(?:[ \t]*--[^\n]*\n)*[ \t]*correlation_id TEXT\n\);",
        "\n);",
        schema_sql,
    )


def _write_pre_t4_chain(db: Path, rows: int = 2) -> list[str]:
    """Materialize a pre-T-4 ledger holding GENUINELY chained rows, hashed by
    the very helpers the store uses, so the chain is authentic rather than
    hand-faked."""
    conn = sqlite3.connect(db)
    conn.executescript(_strip_audit_correlation(SCHEMA_SQL))
    running_prev = ""
    ids: list[str] = []
    for i in range(1, rows + 1):
        payload = {
            "id": f"aud_old{i}",
            "action_id": None,
            "pocket_id": "pocket-old",
            "timestamp": f"2026-07-0{i}T00:00:00",
            "actor": "user:old",
            "event": "action_approved",
            "category": "decision",
            "description": f"old row {i}",
            "context": {},
            "ai_recommendation": None,
            "outcome": None,
        }
        entry_hash = compute_audit_hash(_canonical_audit_payload(**payload), running_prev)
        conn.execute(
            "INSERT INTO instinct_audit"
            " (id, action_id, pocket_id, timestamp, actor, event, category,"
            " description, context, ai_recommendation, outcome, prev_hash, entry_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                payload["id"],
                None,
                payload["pocket_id"],
                payload["timestamp"],
                payload["actor"],
                payload["event"],
                payload["category"],
                payload["description"],
                "{}",
                None,
                None,
                running_prev,
                entry_hash,
            ),
        )
        ids.append(payload["id"])
        running_prev = entry_hash
    conn.commit()
    conn.close()
    return ids


async def test_pre_t4_ledger_verifies_after_migration_and_extends(tmp_path: Path) -> None:
    """The upgrade gate. A ledger written before the column existed opens
    through the guarded ALTER, its existing chain still verifies byte-for-byte,
    and new rows extend the SAME chain while carrying their correlation.

    Drop the T-4 ALTER and this fails at the post-migration append (the INSERT
    names a column the old file doesn't have, which surfaces as
    ``AuditChainError``). Fold correlation_id into the hash material and it
    fails at the first ``verify_audit_chain``.
    """
    db = tmp_path / "instinct_pre_t4.db"
    pre_t4 = _strip_audit_correlation(SCHEMA_SQL)
    # The fixture really is pre-T-4: the audit table has no correlation column,
    # while the T-3 actions columns are untouched.
    assert "correlation_id TEXT\n);" not in pre_t4
    assert "correlation_id TEXT," in pre_t4
    _write_pre_t4_chain(db)

    store = InstinctStore(db)
    verdict = await store.verify_audit_chain()
    assert verdict["intact"] is True
    assert verdict["hashed"] == 2
    assert verdict["checked"] == 2

    action = await store.propose(
        pocket_id="pocket-old",
        title="post-migration",
        description="",
        recommendation="",
        trigger=TRIGGER,
        correlation_id=CORR,
    )
    approved = await store.approve(action.id, approver="maya")
    assert approved is not None

    after = await store.verify_audit_chain()
    assert after["intact"] is True
    assert after["hashed"] == 4
    assert after["checked"] == 4

    # Old rows read NULL — nothing was backfilled or guessed; new rows carry the
    # chain key.
    assert [corr for _id, corr in _audit_rows(store)] == [None, None, CORR, CORR]
