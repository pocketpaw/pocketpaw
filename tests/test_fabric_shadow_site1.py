# tests/test_fabric_shadow_site1.py
# Created: 2026-07-10 (FST-3 — SHADOW mode at merge site 1).
#
# Proves the first real source-truth data flow: store.update_object records
# statements when fabric_source_truth_mode is shadow/enforce, while the LWW
# cache write stays byte-for-byte unchanged in every mode.
#
#   * two writers (different sources) on one property in shadow — both
#     statements exist, the divergence line is logged with correct
#     diverged/disputed/unresolvable values, the cache still shows the LWW
#     value,
#   * single-source / same-source / immaterial-difference / brand-new-key
#     properties — zero statements, zero log lines (the opt-in discipline),
#   * mode=off — byte-for-byte: the mode is read ONCE and no statement verb
#     (get_statements / append_statement / upsert_source) is ever touched,
#     asserted via instance spies + direct table counts,
#   * auto-promotion — the seed statement carries the OLD cache value, the
#     object-level provenance (source_connector -> writer_class "connector",
#     a connector_run SourceRef), and touch-time observed_at backfill (the
#     object's TRUE pre-update updated_at from the DB row),
#   * ingest_records provenance threading — the re-sync UPDATE path produces a
#     statement with writer_class="connector" and a SourceRef carrying the
#     connector name + run_id,
#   * the divergence log line is grep-stable and single-line (FST-8's harness
#     contract), enforce behaves as shadow (until FST-5), a shadow failure
#     never breaks the cache write, and statements/sources are stamped with
#     the caller's workspace_id.

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pocketpaw.connectors.fabric_ingest import FabricMapping, ingest_records
from pocketpaw.fabric.store import FabricStore, _source_truth_mode

STORE_LOGGER = "pocketpaw.fabric.store"

# The FST-8 harness contract: one line, grep-stable, values JSON-encoded.
DIVERGENCE_RE = re.compile(
    r"^fabric shadow: object=\S+ property=\S+ lww=.+ resolver=.+"
    r" diverged=(True|False) disputed=(True|False) unresolvable=(True|False)$"
)


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", lambda: mode)


def _shadow_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The divergence lines (excludes the failure-shield warning)."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == STORE_LOGGER and r.getMessage().startswith("fabric shadow: object=")
    ]


def _table_count(db_path: Path, table: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    finally:
        con.close()


def _db_object_timestamps(db_path: Path, obj_id: str) -> tuple[str, str]:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT created_at, updated_at FROM fabric_objects WHERE id = ?", (obj_id,)
        ).fetchone()
    finally:
        con.close()
    return row[0], row[1]


def _source_row(db_path: Path, source_ref_id: str) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM fabric_sources WHERE id = ?", (source_ref_id,)).fetchone()
    finally:
        con.close()
    assert row is not None, f"no fabric_sources row for {source_ref_id}"
    return dict(row)


async def _crm_object(tmp_path: Path, **props: Any) -> tuple[FabricStore, str, Path]:
    """A store with one connector-owned object; returns (store, object_id, db_path)."""
    db_path = tmp_path / "fabric.db"
    store = FabricStore(db_path)
    obj_type = await store.define_type(name="Customer", properties=[])
    obj = await store.create_object(
        obj_type.id,
        props or {"name": "Acme", "arr": 120},
        source_connector="crm",
        source_id="c-1",
    )
    return store, obj.id, db_path


# ---------------------------------------------------------------------------
# The settings wiring
# ---------------------------------------------------------------------------


def test_mode_helper_reads_settings_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    import pocketpaw.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "get_settings",
        lambda force_reload=False: SimpleNamespace(fabric_source_truth_mode="shadow"),
    )
    assert _source_truth_mode() == "shadow"


# ---------------------------------------------------------------------------
# (c) mode=off — byte-for-byte, mode read once, statements path untouched
# ---------------------------------------------------------------------------


async def test_mode_off_never_touches_statements_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, obj_id, db_path = await _crm_object(tmp_path)

    mode_reads = {"count": 0}

    def counting_off() -> str:
        mode_reads["count"] += 1
        return "off"

    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", counting_off)

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("statements path touched in mode=off")

    monkeypatch.setattr(store, "get_statements", _boom)
    monkeypatch.setattr(store, "append_statement", _boom)
    monkeypatch.setattr(store, "upsert_source", _boom)

    caplog.set_level(logging.INFO, logger=STORE_LOGGER)
    updated = await store.update_object(obj_id, {"arr": 150}, writer_class="agent")

    assert updated is not None
    assert updated.properties == {"name": "Acme", "arr": 150}  # LWW merge intact
    assert mode_reads["count"] == 1  # mode read ONCE per update_object call
    assert _table_count(db_path, "fabric_statements") == 0
    assert _table_count(db_path, "fabric_sources") == 0
    assert _shadow_lines(caplog) == []


# ---------------------------------------------------------------------------
# (a) + (d) two writers, promotion seed, divergence log, cache unchanged
# ---------------------------------------------------------------------------


async def test_shadow_two_writers_promotes_logs_and_keeps_lww_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, obj_id, db_path = await _crm_object(tmp_path)
    _, pre_updated_at = _db_object_timestamps(db_path, obj_id)

    _set_mode(monkeypatch, "shadow")
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)

    # Second distinct source: an agent session, vs the object's crm connector.
    await store.update_object(
        obj_id, {"arr": 150}, writer_class="agent", source_session_id="sess-1"
    )

    # Cache: EXACTLY the LWW value, shadow never changes it.
    obj = await store.get_object(obj_id)
    assert obj is not None
    assert obj.properties["arr"] == 150

    # Both statements exist: the promoted seed + the incoming write.
    stmts = await store.get_statements(obj_id, "arr")
    assert len(stmts) == 2
    seed = next(s for s in stmts if s.writer_class == "connector")
    incoming = next(s for s in stmts if s.writer_class == "agent")

    # (d) the seed carries the OLD cache value, object-level provenance, and
    # touch-time backfill = the object's TRUE pre-update updated_at.
    assert seed.value == 120
    assert seed.observed_at == datetime.fromisoformat(pre_updated_at)
    seed_src = _source_row(db_path, seed.source_ref_id)
    assert seed_src["kind"] == "connector_run"
    assert seed_src["connector"] == "crm"
    assert seed_src["run_id"] is None

    # The incoming statement carries the caller's provenance.
    assert incoming.value == 150
    in_src = _source_row(db_path, incoming.source_ref_id)
    assert in_src["kind"] == "agent_session"
    assert in_src["session_id"] == "sess-1"

    # ONE divergence line, exact FST-8 shape. The connector-tier seed (120)
    # outranks the agent write on the trust ladder, so the resolver diverges
    # from the LWW cache (150), and the open agent loser disputes.
    lines = _shadow_lines(caplog)
    assert lines == [
        f"fabric shadow: object={obj_id} property=arr lww=150 resolver=120"
        " diverged=True disputed=True unresolvable=False"
    ]
    assert DIVERGENCE_RE.fullmatch(lines[0])
    assert "\n" not in lines[0]


# ---------------------------------------------------------------------------
# (b) the opt-in discipline: untracked single-source properties stay scalar
# ---------------------------------------------------------------------------


async def test_shadow_same_connector_resync_writes_no_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, obj_id, db_path = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "shadow")
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)

    # Same source as the object's owner: crm refreshing its own object.
    await store.update_object(
        obj_id, {"arr": 999}, writer_class="connector", source_connector="crm"
    )

    assert _table_count(db_path, "fabric_statements") == 0
    assert _table_count(db_path, "fabric_sources") == 0
    assert _shadow_lines(caplog) == []
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 999


async def test_shadow_unattributed_write_derives_to_owner_no_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No provenance kwargs on a connector-owned object derives to that same
    connector (the honest default — the historical caller IS the re-sync), so
    it is NOT a second distinct source and nothing is tracked."""
    store, obj_id, db_path = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "shadow")
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)

    await store.update_object(obj_id, {"arr": 500})

    assert _table_count(db_path, "fabric_statements") == 0
    assert _shadow_lines(caplog) == []


async def test_shadow_agent_object_agent_write_no_statements(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unattributed object (no source_connector) written by an agent: the
    baseline writer class is 'agent' and so is the incoming — same source."""
    db_path = tmp_path / "fabric.db"
    store = FabricStore(db_path)
    obj_type = await store.define_type(name="Note", properties=[])
    obj = await store.create_object(obj_type.id, {"body": "draft"})
    _set_mode(monkeypatch, "shadow")

    await store.update_object(obj.id, {"body": "final"}, writer_class="agent")

    assert _table_count(db_path, "fabric_statements") == 0


async def test_shadow_promotion_requires_material_difference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id, db_path = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "shadow")

    # Same value from a second source — nothing to dispute, no promotion.
    await store.update_object(obj_id, {"arr": 120}, writer_class="agent")
    # Whitespace-only string difference is immaterial by the resolver's rule.
    await store.update_object(obj_id, {"name": "  Acme  "}, writer_class="agent")

    assert _table_count(db_path, "fabric_statements") == 0


async def test_shadow_new_key_from_second_source_not_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A brand-new property has no prior cache claim to preserve — it stays
    untracked even when written by a second distinct source."""
    store, obj_id, db_path = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "shadow")
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)

    await store.update_object(obj_id, {"tier": "gold"}, writer_class="agent")

    assert _table_count(db_path, "fabric_statements") == 0
    assert _shadow_lines(caplog) == []


# ---------------------------------------------------------------------------
# Tracked properties append on EVERY write; mixed updates log per-statement
# ---------------------------------------------------------------------------


async def test_shadow_tracked_property_appends_and_reconverges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, obj_id, db_path = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "shadow")
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)

    # Promote via an agent write (2 statements: seed 120 + agent 150).
    await store.update_object(obj_id, {"arr": 150}, writer_class="agent")
    caplog.clear()

    # Now the owning connector writes again — tracked, so it appends even from
    # the same source. Far-future observed_at keeps the recency leg (and the
    # 24h un-rankable epsilon) deterministic regardless of run date/timezone.
    await store.update_object(
        obj_id,
        {"arr": 200},
        writer_class="connector",
        source_connector="crm",
        source_run_id="run-9",
        observed_at=datetime(2030, 1, 1, 12, 0, 0),
    )

    stmts = await store.get_statements(obj_id, "arr")
    assert len(stmts) == 3
    lines = _shadow_lines(caplog)
    # The fresh connector write wins the ladder → resolver agrees with LWW.
    assert lines == [
        f"fabric shadow: object={obj_id} property=arr lww=200 resolver=200"
        " diverged=False disputed=True unresolvable=False"
    ]


async def test_shadow_mixed_update_logs_only_statement_properties(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, obj_id, db_path = await _crm_object(tmp_path, name="Acme", arr=120, note="x")
    _set_mode(monkeypatch, "shadow")
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)

    # Track arr via promotion.
    await store.update_object(obj_id, {"arr": 150}, writer_class="agent")
    caplog.clear()

    # Same-source update touching a tracked (arr) and an untracked (note)
    # property: arr appends + logs, note stays scalar — one line total.
    await store.update_object(
        obj_id,
        {"arr": 170, "note": "z"},
        writer_class="connector",
        source_connector="crm",
    )

    assert len(await store.get_statements(obj_id, "arr")) == 3
    assert await store.get_statements(obj_id, "note") == []
    lines = _shadow_lines(caplog)
    assert len(lines) == 1 and "property=arr" in lines[0]


# ---------------------------------------------------------------------------
# (e) ingest_records threads connector provenance end-to-end
# ---------------------------------------------------------------------------


async def test_ingest_records_threads_provenance_through_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "fabric.db"
    store = FabricStore(db_path)
    _set_mode(monkeypatch, "shadow")
    mapping = FabricMapping(
        type_name="CalendarEvent",
        source_id_field="event_id",
        field_map={"title": "title"},
    )

    # CREATE path — no statements at site 1 (creates are FST-4's scope).
    res1 = await ingest_records(store, "gcal", [{"event_id": "e1", "title": "Standup"}], mapping)
    assert res1.created == 1
    obj_id = res1.object_ids[0]
    assert await store.get_statements(obj_id, "title") == []

    # An agent edit promotes the property (seed "Standup" + agent value).
    await store.update_object(
        obj_id, {"title": "Standup (moved)"}, writer_class="agent", source_session_id="s-9"
    )
    assert len(await store.get_statements(obj_id, "title")) == 2

    # Re-sync: the UPDATE path passes connector provenance + run identity.
    res2 = await ingest_records(
        store, "gcal", [{"event_id": "e1", "title": "Standup v2"}], mapping, run_id="run-7"
    )
    assert res2.updated == 1

    stmts = await store.get_statements(obj_id, "title")
    assert len(stmts) == 3
    ingest_stmt = next(s for s in stmts if s.value == "Standup v2")
    assert ingest_stmt.writer_class == "connector"
    src = _source_row(db_path, ingest_stmt.source_ref_id)
    assert src["kind"] == "connector_run"
    assert src["connector"] == "gcal"
    assert src["run_id"] == "run-7"

    # Cache is still plain LWW.
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["title"] == "Standup v2"


# ---------------------------------------------------------------------------
# enforce == shadow (until FST-5); shadow failures never break the write
# ---------------------------------------------------------------------------


async def test_enforce_mode_behaves_as_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, obj_id, db_path = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "enforce")

    await store.update_object(obj_id, {"arr": 150}, writer_class="agent")

    assert len(await store.get_statements(obj_id, "arr")) == 2
    obj = await store.get_object(obj_id)
    assert obj is not None and obj.properties["arr"] == 150  # cache still LWW


async def test_shadow_failure_never_breaks_cache_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store, obj_id, _ = await _crm_object(tmp_path)
    _set_mode(monkeypatch, "shadow")

    async def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated shadow failure")

    monkeypatch.setattr(store, "append_statement", _explode)
    caplog.set_level(logging.WARNING, logger=STORE_LOGGER)

    updated = await store.update_object(obj_id, {"arr": 150}, writer_class="agent")

    assert updated is not None and updated.properties["arr"] == 150
    assert any(
        "statement pass failed" in r.getMessage() for r in caplog.records if r.name == STORE_LOGGER
    )


# ---------------------------------------------------------------------------
# Workspace stamping (W4a semantics carried into the shadow pass)
# ---------------------------------------------------------------------------


async def test_shadow_statements_and_sources_stamped_with_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "fabric.db"
    store = FabricStore(db_path)
    obj_type = await store.define_type(name="Customer", properties=[], workspace_id="ws-a")
    obj = await store.create_object(
        obj_type.id,
        {"arr": 120},
        source_connector="crm",
        source_id="c-1",
        workspace_id="ws-a",
    )
    _set_mode(monkeypatch, "shadow")

    await store.update_object(obj.id, {"arr": 150}, workspace_id="ws-a", writer_class="agent")

    stmts = await store.get_statements(obj.id, "arr", workspace_id="ws-a")
    assert len(stmts) == 2
    assert all(s.workspace_id == "ws-a" for s in stmts)
    for s in stmts:
        assert _source_row(db_path, s.source_ref_id)["workspace_id"] == "ws-a"
