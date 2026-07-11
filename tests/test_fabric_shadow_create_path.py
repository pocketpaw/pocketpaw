# tests/test_fabric_shadow_create_path.py
# Created: 2026-07-10 (FST-4 — the CREATE-path decision + the mirror writer
# family).
#
# FST-3 left ingest_records' CREATE path without a statement hook. FST-4's
# ruling: creates need NO hook — promotion-time seeding already preserves the
# create-time claim. This file is the PROOF that backs the decision:
#
#   * create-then-update — an object created by source A (via ingest_records,
#     zero statements at create time even in shadow) then updated by source B
#     yields BOTH claims: B's statement plus A's create-time value seeded with
#     A's provenance and touch-time observed_at. The conflict is visible
#     (disputed=True on the divergence line) — nothing was lost by skipping a
#     create hook.
#
# Plus the FST-4 writer-family rule and per-record provenance threading:
#
#   * mirror self-refresh — writer_class="mirror" from the object's OWN
#     connector is the owning sync, not a second source: no promotion, the
#     opt-in discipline holds for mirrored data,
#   * mirror on a FOREIGN connector's object — a real second source: promotes,
#     and the statement records the true writer_class ("mirror"),
#   * ingest_records' document_uri_field / observed_at_field — per-record
#     SourceRef document identity and source-time observed_at; non-datetime
#     observed values are ignored (ingest-time default).

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from pocketpaw.connectors.fabric_ingest import FabricMapping, ingest_records
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


async def _source(store: FabricStore, source_ref_id: str) -> dict[str, Any]:
    import sqlite3

    con = sqlite3.connect(store._db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM fabric_sources WHERE id = ?", (source_ref_id,)).fetchone()
    finally:
        con.close()
    assert row is not None
    return dict(row)


_MAPPING = FabricMapping(
    type_name="Customer",
    source_id_field="ext_id",
    field_map={"name": "name", "arr": "arr"},
)


# ---------------------------------------------------------------------------
# THE create-path proof: create by A, update by B → both claims, no hook needed
# ---------------------------------------------------------------------------


async def test_create_then_update_yields_both_claims_without_a_create_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = FabricStore(tmp_path / "fabric.db")
    _set_mode(monkeypatch, "shadow")  # shadow from the very start — creates included
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)

    # Source A (the crm connector) CREATES the object. Even in shadow mode the
    # create path appends nothing — that is the documented decision under test.
    res = await ingest_records(
        store, "crm", [{"ext_id": "c-1", "name": "Acme", "arr": 120}], _MAPPING
    )
    assert res.created == 1
    obj_id = res.object_ids[0]
    assert await store.get_statements(obj_id, "arr") == []
    assert _shadow_lines(caplog) == []

    # Source B (an agent) UPDATES the property A created.
    await store.update_object(
        obj_id, {"arr": 250}, writer_class="agent", source_session_id="sess-b"
    )

    # BOTH claims are present: A's create-time value was seeded at promotion
    # time with A's provenance — no create hook was needed to preserve it.
    stmts = await store.get_statements(obj_id, "arr")
    assert len(stmts) == 2
    claim_a = next(s for s in stmts if s.writer_class == "connector")
    claim_b = next(s for s in stmts if s.writer_class == "agent")
    assert claim_a.value == 120  # the create-time claim, preserved
    assert claim_b.value == 250
    src_a = await _source(store, claim_a.source_ref_id)
    assert src_a["kind"] == "connector_run" and src_a["connector"] == "crm"

    # And the conflict is VISIBLE: one divergence line, disputed=True.
    lines = _shadow_lines(caplog)
    assert len(lines) == 1
    assert re.search(r" disputed=True ", lines[0] + " ")
    assert f"object={obj_id} property=arr" in lines[0]


# ---------------------------------------------------------------------------
# The writer-family rule: mirror vs its own connector is NOT a second source
# ---------------------------------------------------------------------------


async def test_mirror_self_refresh_does_not_promote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = FabricStore(tmp_path / "fabric.db")
    _set_mode(monkeypatch, "shadow")
    caplog.set_level(logging.INFO, logger=STORE_LOGGER)

    # The mirror creates, then re-syncs the SAME object with a changed value.
    await ingest_records(
        store, "firestore", [{"ext_id": "col/d1", "name": "Acme", "arr": 120}], _MAPPING
    )
    res = await ingest_records(
        store,
        "firestore",
        [{"ext_id": "col/d1", "name": "Acme", "arr": 500}],
        _MAPPING,
        writer_class="mirror",
    )
    assert res.updated == 1
    obj = await store.get_object_by_source("firestore", "col/d1")
    assert obj is not None and obj.properties["arr"] == 500  # LWW refresh intact

    # Same connector + machine-sync family → the owning sync, not a second
    # source: no statements, no log line (the opt-in discipline holds).
    assert await store.get_statements(obj.id, "arr") == []
    assert _shadow_lines(caplog) == []


async def test_mirror_write_on_foreign_object_is_a_second_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FabricStore(tmp_path / "fabric.db")
    _set_mode(monkeypatch, "shadow")

    # Object owned by the crm connector; the firestore mirror writes it.
    res = await ingest_records(
        store, "crm", [{"ext_id": "c-9", "name": "Acme", "arr": 120}], _MAPPING
    )
    obj_id = res.object_ids[0]
    await store.update_object(
        obj_id,
        {"arr": 300},
        writer_class="mirror",
        source_connector="firestore",
        source_document_uri="customers/c-9",
    )

    stmts = await store.get_statements(obj_id, "arr")
    assert len(stmts) == 2
    mirror = next(s for s in stmts if s.writer_class == "mirror")  # true class recorded
    src = await _source(store, mirror.source_ref_id)
    assert src["connector"] == "firestore"
    assert src["document_uri"] == "customers/c-9"


# ---------------------------------------------------------------------------
# ingest_records per-record provenance threading (document URI + observed_at)
# ---------------------------------------------------------------------------


async def test_ingest_records_threads_document_uri_and_observed_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = FabricStore(tmp_path / "fabric.db")
    _set_mode(monkeypatch, "shadow")

    res = await ingest_records(
        store, "crm", [{"ext_id": "c-2", "name": "Acme", "arr": 120}], _MAPPING
    )
    obj_id = res.object_ids[0]
    # Promote arr via a second source so subsequent writes append.
    await store.update_object(obj_id, {"arr": 130}, writer_class="agent")
    assert len(await store.get_statements(obj_id, "arr")) == 2

    source_time = datetime(2030, 5, 1, 9, 30, 0)
    record = {
        "ext_id": "c-2",
        "name": "Acme",
        "arr": 200,
        "__uri__": "crm://accounts/c-2",
        "__obs__": source_time,
    }
    await ingest_records(
        store,
        "crm",
        [record],
        _MAPPING,
        document_uri_field="__uri__",
        observed_at_field="__obs__",
    )

    stmts = await store.get_statements(obj_id, "arr")
    latest = next(s for s in stmts if s.value == 200)
    assert latest.observed_at == source_time  # the SOURCE's time, not ingest time
    src = await _source(store, latest.source_ref_id)
    assert src["document_uri"] == "crm://accounts/c-2"

    # A non-datetime observed value is ignored → ingest-time default (recent).
    record2 = {"ext_id": "c-2", "name": "Acme", "arr": 210, "__obs__": "not-a-datetime"}
    await ingest_records(store, "crm", [record2], _MAPPING, observed_at_field="__obs__")
    newest = next(s for s in await store.get_statements(obj_id, "arr") if s.value == 210)
    assert newest.observed_at != "not-a-datetime"
    assert abs((datetime.now(newest.observed_at.tzinfo) - newest.observed_at).days) < 1
