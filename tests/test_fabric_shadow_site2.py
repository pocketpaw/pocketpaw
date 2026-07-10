# tests/test_fabric_shadow_site2.py
# Created: 2026-07-10 (FST-4 — SHADOW mode at merge site 2).
#
# Proves the journal-replay merge site (fabric/projection.py::_apply_updated,
# the ``merged = {**existing.obj.properties, **patch}`` fold) emits shadow
# statements through the SAME FST-3 store machinery as site 1, with
# journal-derived provenance and replay idempotence:
#
#   * a live journal update by a ``user`` actor in shadow lands both claims
#     (the promoted seed with the object's connector baseline + the human
#     statement with a human_actor SourceRef) and the FST-8 divergence line;
#     observed_at is the EVENT's ts (create ts for the seed, update ts for
#     the incoming claim),
#   * an ``agent`` actor maps to an agent_session SourceRef keyed on the
#     event's correlation_id,
#   * a ``system`` actor (the journal store default) passes NO provenance —
#     the store's derivation default applies, so a system refresh of a
#     connector-owned object is not a second source and stays scalar,
#   * REPLAY DEDUPE — rebuilding/replaying the same journal (same store or a
#     brand-new FabricJournalStore) never double-appends: statements are
#     recorded at most once per journal event id (fabric_shadow_events),
#   * REPLAY DETERMINISM — replaying a journal into a FRESH statement store
#     produces the same claims (property, value, writer_class, observed_at)
#     the live shadow pass produced,
#   * mode=off — byte-for-byte: nothing staged, the statement verbs never
#     touched, the mode read ONCE per update event; a store-LESS projection
#     never even reads the mode flag.

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from soul_protocol.engine.journal import open_journal
from soul_protocol.spec.journal import Actor

from pocketpaw.fabric.events import ACTION_OBJECT_CREATED, ACTION_OBJECT_UPDATED
from pocketpaw.fabric.journal_store import FabricJournalStore
from pocketpaw.fabric.models import FabricObject
from pocketpaw.fabric.store import FabricStore

STORE_LOGGER = "pocketpaw.fabric.store"


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", lambda: mode)


def _shadow_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == STORE_LOGGER and r.getMessage().startswith("fabric shadow: object=")
    ]


def _table_count(db_path: Path, table: str) -> int:
    """Rows in ``table`` — 0 when the table (or the whole DB) doesn't exist
    yet, which is exactly what a byte-for-byte 'off' run leaves behind: the
    store's schema is created lazily on first write, so an untouched
    statement store never even materializes the tables."""
    con = sqlite3.connect(db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


def _source_row(db_path: Path, source_ref_id: str) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM fabric_sources WHERE id = ?", (source_ref_id,)).fetchone()
    finally:
        con.close()
    assert row is not None, f"no fabric_sources row for {source_ref_id}"
    return dict(row)


def _events(journal, action: str) -> list:
    return [e for e in journal.replay_from(0) if e.action == action]


@pytest.fixture
def journal(tmp_path: Path):
    j = open_journal(tmp_path / "journal.db")
    yield j
    j.close()


def _crm_obj(**props: Any) -> FabricObject:
    return FabricObject(
        type_id="t1",
        type_name="Customer",
        properties=props or {"arr": 120},
        source_connector="crm",
        source_id="c-1",
    )


def _human(actor_id: str = "user:alice") -> Actor:
    return Actor(kind="user", id=actor_id, scope_context=[])


# ---------------------------------------------------------------------------
# Live journal update in shadow: journal-derived provenance + divergence line
# ---------------------------------------------------------------------------


async def test_journal_update_by_user_actor_records_both_claims(
    tmp_path: Path,
    journal,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    statement_db = tmp_path / "fabric.db"
    statement_store = FabricStore(statement_db)
    js = FabricJournalStore(journal, statement_store=statement_store)
    js.bootstrap()
    _set_mode(monkeypatch, "shadow")
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)

    obj = await js.create(_crm_obj(arr=120), scope=["org"])
    await js.update(obj.id, {"arr": 150}, scope=["org"], actor=_human())

    # The projection cache is plain LWW — shadow never touches it.
    projected = await js.get(obj.id)
    assert projected is not None and projected.properties["arr"] == 150

    # Both claims exist: the promoted connector seed + the human statement.
    stmts = await statement_store.get_statements(obj.id, "arr")
    assert len(stmts) == 2
    seed = next(s for s in stmts if s.writer_class == "connector")
    human = next(s for s in stmts if s.writer_class == "human")

    create_ts = _events(journal, ACTION_OBJECT_CREATED)[0].ts
    update_ts = _events(journal, ACTION_OBJECT_UPDATED)[0].ts

    # Seed: old cache value, object-level baseline, touch-time = create event ts.
    assert seed.value == 120
    assert seed.observed_at == create_ts
    seed_src = _source_row(statement_db, seed.source_ref_id)
    assert seed_src["kind"] == "connector_run"
    assert seed_src["connector"] == "crm"

    # Human claim: journal-derived provenance, observed_at = the update event ts.
    assert human.value == 150
    assert human.observed_at == update_ts
    human_src = _source_row(statement_db, human.source_ref_id)
    assert human_src["kind"] == "human_actor"
    assert human_src["actor_id"] == "user:alice"

    # ONE divergence line, exact FST-8 shape. Human outranks the connector
    # seed on the ladder, so the resolver agrees with the LWW cache.
    lines = _shadow_lines(caplog)
    assert lines == [
        f"fabric shadow: object={obj.id} property=arr lww=150 resolver=150"
        " diverged=False disputed=True unresolvable=False"
    ]


async def test_agent_actor_maps_to_agent_session_via_correlation_id(
    tmp_path: Path, journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement_db = tmp_path / "fabric.db"
    statement_store = FabricStore(statement_db)
    js = FabricJournalStore(journal, statement_store=statement_store)
    js.bootstrap()
    _set_mode(monkeypatch, "shadow")

    obj = await js.create(_crm_obj(arr=120), scope=["org"])
    correlation = uuid4()
    await js.update(
        obj.id,
        {"arr": 175},
        scope=["org"],
        actor=Actor(kind="agent", id="did:soul:worker", scope_context=[]),
        correlation_id=correlation,
    )

    stmts = await statement_store.get_statements(obj.id, "arr")
    assert len(stmts) == 2
    agent = next(s for s in stmts if s.writer_class == "agent")
    src = _source_row(statement_db, agent.source_ref_id)
    assert src["kind"] == "agent_session"
    assert src["session_id"] == str(correlation)


async def test_system_actor_uses_derivation_default_no_statements(
    tmp_path: Path, journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journal store's default actor is system:fabric — an unattributed
    subsystem write. The store's derivation default maps it to the object's
    own connector, which is NOT a second source, so nothing is tracked."""
    statement_db = tmp_path / "fabric.db"
    statement_store = FabricStore(statement_db)
    js = FabricJournalStore(journal, statement_store=statement_store)
    js.bootstrap()
    _set_mode(monkeypatch, "shadow")

    obj = await js.create(_crm_obj(arr=120), scope=["org"])
    await js.update(obj.id, {"arr": 999}, scope=["org"])  # default system actor

    assert _table_count(statement_db, "fabric_statements") == 0
    assert _table_count(statement_db, "fabric_sources") == 0


# ---------------------------------------------------------------------------
# Replay dedupe: the same journal event never double-appends
# ---------------------------------------------------------------------------


async def test_rebuild_and_replay_never_double_append(
    tmp_path: Path, journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement_db = tmp_path / "fabric.db"
    statement_store = FabricStore(statement_db)
    js = FabricJournalStore(journal, statement_store=statement_store)
    js.bootstrap()
    _set_mode(monkeypatch, "shadow")

    obj = await js.create(_crm_obj(arr=120), scope=["org"])
    await js.update(obj.id, {"arr": 150}, scope=["org"], actor=_human())
    assert len(await statement_store.get_statements(obj.id, "arr")) == 2

    # Rebuild the SAME store's projection from genesis: the update event is
    # staged again, but its id is already claimed — flush records nothing.
    js.bootstrap()
    assert await js.flush_shadow() == 0
    assert len(await statement_store.get_statements(obj.id, "arr")) == 2

    # A brand-new FabricJournalStore over the same journal + statement store
    # (a process restart): same outcome, still exactly two claims.
    js2 = FabricJournalStore(journal, statement_store=statement_store)
    js2.bootstrap()
    assert await js2.flush_shadow() == 0
    assert len(await statement_store.get_statements(obj.id, "arr")) == 2
    assert _table_count(statement_db, "fabric_shadow_events") == 1


# ---------------------------------------------------------------------------
# Replay determinism: a replayed event produces the claims the live write did
# ---------------------------------------------------------------------------


async def test_replay_into_fresh_store_matches_live_statements(
    tmp_path: Path, journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    live_store = FabricStore(tmp_path / "live.db")
    js = FabricJournalStore(journal, statement_store=live_store)
    js.bootstrap()
    _set_mode(monkeypatch, "shadow")

    obj = await js.create(_crm_obj(arr=120, name="Acme"), scope=["org"])
    await js.update(obj.id, {"arr": 150}, scope=["org"], actor=_human())
    live = await live_store.get_statements(obj.id, "arr")
    assert len(live) == 2

    # Replay the same journal into a FRESH statement store (new machine, empty
    # statements DB): bootstrap stages the update, flush records it.
    replay_store = FabricStore(tmp_path / "replay.db")
    js2 = FabricJournalStore(journal, statement_store=replay_store)
    js2.bootstrap()
    assert await js2.flush_shadow() == 1
    replayed = await replay_store.get_statements(obj.id, "arr")

    def _claims(stmts: list) -> set[tuple]:
        return {(s.property, s.value, s.writer_class, s.observed_at) for s in stmts}

    assert _claims(replayed) == _claims(live)


# ---------------------------------------------------------------------------
# mode=off — byte-for-byte at the projection site
# ---------------------------------------------------------------------------


async def test_mode_off_stages_nothing_and_never_touches_statement_verbs(
    tmp_path: Path, journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    statement_db = tmp_path / "fabric.db"
    statement_store = FabricStore(statement_db)
    js = FabricJournalStore(journal, statement_store=statement_store)
    js.bootstrap()

    obj = await js.create(_crm_obj(arr=120), scope=["org"])

    mode_reads = {"count": 0}

    def counting_off() -> str:
        mode_reads["count"] += 1
        return "off"

    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", counting_off)

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("statements path touched in mode=off")

    monkeypatch.setattr(statement_store, "get_statements", _boom)
    monkeypatch.setattr(statement_store, "append_statement", _boom)
    monkeypatch.setattr(statement_store, "upsert_source", _boom)
    monkeypatch.setattr(statement_store, "shadow_record_event_update", _boom)

    updated = await js.update(obj.id, {"arr": 150}, scope=["org"], actor=_human())

    assert updated is not None and updated.properties["arr"] == 150  # LWW fold intact
    assert mode_reads["count"] == 1  # read ONCE per update event, at stage time
    assert await js.flush_shadow() == 0  # nothing was staged
    assert _table_count(statement_db, "fabric_statements") == 0
    assert _table_count(statement_db, "fabric_shadow_events") == 0  # not even the marker


async def test_storeless_projection_never_reads_the_mode_flag(
    journal, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default construction (no statement_store) must be byte-for-byte the
    pre-FST-4 projection: the statement_store gate comes before the mode
    read, so the flag is never consulted."""

    def _explode() -> str:
        raise AssertionError("mode flag read by a store-less projection")

    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", _explode)

    js = FabricJournalStore(journal)  # no statement_store
    js.bootstrap()
    obj = await js.create(_crm_obj(arr=120), scope=["org"])
    updated = await js.update(obj.id, {"arr": 150}, scope=["org"], actor=_human())
    assert updated is not None and updated.properties["arr"] == 150
    assert await js.flush_shadow() == 0
