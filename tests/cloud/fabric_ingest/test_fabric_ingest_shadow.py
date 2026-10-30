# tests/cloud/fabric_ingest/test_fabric_ingest_shadow.py
# Created: 2026-07-10 (FST-4 — SHADOW mode at merge site 3, the Firestore
# mirror worker).
#
# Proves the EE Firestore→Fabric mirror threads TRUE provenance into the
# shared shadow machinery via the canonical OSS ingest loop:
#
#   * a mirror re-sync of a TRACKED property lands a statement with
#     writer_class="mirror" and a SourceRef naming the exact Firestore
#     collection/doc (kind=connector_run, connector="firestore",
#     document_uri=<doc path>), workspace-stamped,
#   * observed_at is the best available SOURCE time: the doc's cursor_field
#     value when it parses, the snapshot update_time as fallback — never
#     fabricated (see service._doc_observed_at's audit),
#   * a mirror self-refresh does NOT promote (writer-family rule): the
#     mirror refreshing its own objects keeps them scalar/cheap,
#   * mirror-create then agent-update yields BOTH claims (the ee variant of
#     the FST-4 create-path proof),
#   * mode=off — byte-for-byte: objects mirror exactly as before, zero
#     statements/sources rows,
#   * tenant scoping — statements + sources carry the workspace; a read
#     scoped to another tenant sees none of them.
#
# Same harness as test_fabric_ingest_service.py: fake reader, real FabricStore
# on tmp SQLite, config/state in the mongo_db fixture — no google creds.

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.fabric_ingest import service as ingest_service  # noqa: E402
from pocketpaw_ee.cloud.models.fabric_ingest_state import (  # noqa: E402
    FabricFieldMapping,
    FabricIngestConfig,
)

from pocketpaw.fabric.models import PropertyDef  # noqa: E402
from pocketpaw.fabric.store import FabricStore  # noqa: E402

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fakes + helpers (mirrors test_fabric_ingest_service.py).
# --------------------------------------------------------------------------


class FakeFirestoreReader:
    def __init__(self, docs: list[dict] | None = None) -> None:
        self._docs = docs or []

    async def read_collection(self, collection, *, cursor_field, cursor, limit):  # noqa: ARG002
        out = []
        for d in self._docs:
            val = (d.get("data") or {}).get(cursor_field)
            if val is None or val == "":
                val = d.get("update_time") or ""
            if cursor and str(val) <= cursor:
                continue
            out.append(d)
        return out[:limit]


def _doc(path: str, **fields) -> dict:
    update_time = fields.pop("_update_time", "2026-06-01T00:00:00Z")
    return {"path": path, "data": dict(fields), "update_time": update_time}


async def _make_store(tmp_path) -> FabricStore:
    store = FabricStore(tmp_path / "fabric.db")
    await store.define_type("Customer", [PropertyDef(name="name")])
    return store


async def _seed_config(workspace_id: str, mappings: list[FabricFieldMapping]) -> None:
    cfg = FabricIngestConfig(workspace=workspace_id, enabled=True, mappings=mappings)
    await cfg.insert()


def _customer_mapping() -> FabricFieldMapping:
    return FabricFieldMapping(
        collection="customers",
        object_type_id="ot-customer",
        field_map={"display_name": "name", "tier": "tier"},
        cursor_field="updated_at",
    )


def _set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setattr("pocketpaw.fabric.store._source_truth_mode", lambda: mode)


def _source_row(store: FabricStore, source_ref_id: str) -> dict:
    con = sqlite3.connect(store._db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM fabric_sources WHERE id = ?", (source_ref_id,)).fetchone()
    finally:
        con.close()
    assert row is not None
    return dict(row)


def _table_count(store: FabricStore, table: str) -> int:
    con = sqlite3.connect(store._db_path)
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
    except sqlite3.OperationalError:
        return 0
    finally:
        con.close()


# --------------------------------------------------------------------------
# Mirror provenance: writer_class="mirror" + firestore doc identity + times.
# --------------------------------------------------------------------------


async def test_mirror_resync_lands_mirror_statement_with_firestore_identity(
    mongo_db,
    tmp_path,
    monkeypatch,  # noqa: ARG001
):
    store = await _make_store(tmp_path)
    ws = "w1"
    await _seed_config(ws, [_customer_mapping()])
    _set_mode(monkeypatch, "shadow")

    # Backfill mirrors the doc (CREATE — no statements, by the FST-4 ruling).
    reader1 = FakeFirestoreReader(
        [_doc("customers/c1", display_name="Acme", tier="gold", updated_at="2026-06-02T10:00:00Z")]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader1, store=store)
    obj = await store.get_object_by_source("firestore", "customers/c1", workspace_id=ws)
    assert obj is not None
    assert await store.get_statements(obj.id, "name", workspace_id=ws) == []

    # An agent edit promotes "name" (seed from the firestore baseline + agent).
    await store.update_object(
        obj.id,
        {"name": "Acme (agent edit)"},
        workspace_id=ws,
        writer_class="agent",
        source_session_id="sess-1",
    )
    assert len(await store.get_statements(obj.id, "name", workspace_id=ws)) == 2

    # Incremental re-sync with a newer doc version: the mirror UPDATE path.
    reader2 = FakeFirestoreReader(
        [
            _doc(
                "customers/c1",
                display_name="Acme v2",
                tier="gold",
                updated_at="2026-06-05T10:00:00Z",
            )
        ]
    )
    result = await ingest_service.ingest_collection(ws, "customers", reader=reader2, store=store)
    assert result["status"] == "ok" and result["mode"] == "incremental"

    stmts = await store.get_statements(obj.id, "name", workspace_id=ws)
    assert len(stmts) == 3
    mirror = next(s for s in stmts if s.writer_class == "mirror")
    assert mirror.value == "Acme v2"
    # observed_at = the doc's cursor_field time (the REAL source time).
    assert mirror.observed_at == datetime.fromisoformat("2026-06-05T10:00:00Z")
    src = _source_row(store, mirror.source_ref_id)
    assert src["kind"] == "connector_run"
    assert src["connector"] == "firestore"
    assert src["document_uri"] == "customers/c1"  # the exact collection/doc
    assert src["workspace_id"] == ws
    assert mirror.workspace_id == ws

    # The untracked "tier" property stayed scalar (opt-in discipline), and the
    # cache is plain LWW.
    assert await store.get_statements(obj.id, "tier", workspace_id=ws) == []
    refreshed = await store.get_object(obj.id, workspace_id=ws)
    assert refreshed is not None and refreshed.properties["name"] == "Acme v2"

    # Tenant scoping: another workspace's scoped read sees NONE of it.
    assert await store.get_statements(obj.id, "name", workspace_id="w-other") == []


async def test_observed_at_falls_back_to_snapshot_update_time(
    mongo_db,
    tmp_path,
    monkeypatch,  # noqa: ARG001
):
    store = await _make_store(tmp_path)
    ws = "w1"
    await _seed_config(ws, [_customer_mapping()])
    _set_mode(monkeypatch, "shadow")

    # Doc WITHOUT the cursor field: update_time is the best available time.
    reader1 = FakeFirestoreReader(
        [_doc("customers/c1", display_name="Acme", _update_time="2026-06-03T08:00:00Z")]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader1, store=store)
    obj = await store.get_object_by_source("firestore", "customers/c1", workspace_id=ws)
    await store.update_object(obj.id, {"name": "Agent"}, workspace_id=ws, writer_class="agent")

    reader2 = FakeFirestoreReader(
        [_doc("customers/c1", display_name="Acme v2", _update_time="2026-06-07T08:00:00Z")]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader2, store=store)

    stmts = await store.get_statements(obj.id, "name", workspace_id=ws)
    mirror = next(s for s in stmts if s.writer_class == "mirror")
    assert mirror.observed_at == datetime.fromisoformat("2026-06-07T08:00:00Z")


# --------------------------------------------------------------------------
# Writer-family rule at the worker level: self-refresh stays scalar.
# --------------------------------------------------------------------------


async def test_mirror_self_refresh_writes_no_statements(
    mongo_db,
    tmp_path,
    monkeypatch,  # noqa: ARG001
):
    store = await _make_store(tmp_path)
    ws = "w1"
    await _seed_config(ws, [_customer_mapping()])
    _set_mode(monkeypatch, "shadow")

    reader1 = FakeFirestoreReader(
        [_doc("customers/c1", display_name="Acme", updated_at="2026-06-02T00:00:00Z")]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader1, store=store)
    # The same doc changes upstream; the mirror refreshes its OWN object.
    reader2 = FakeFirestoreReader(
        [_doc("customers/c1", display_name="Acme Renamed", updated_at="2026-06-04T00:00:00Z")]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader2, store=store)

    obj = await store.get_object_by_source("firestore", "customers/c1", workspace_id=ws)
    assert obj.properties["name"] == "Acme Renamed"  # LWW refresh intact
    assert _table_count(store, "fabric_statements") == 0  # no promotion noise


# --------------------------------------------------------------------------
# The ee create-then-update proof: mirror creates, agent updates → conflict.
# --------------------------------------------------------------------------


async def test_mirror_create_then_agent_update_conflict_visible(
    mongo_db,
    tmp_path,
    monkeypatch,  # noqa: ARG001
):
    store = await _make_store(tmp_path)
    ws = "w1"
    await _seed_config(ws, [_customer_mapping()])
    _set_mode(monkeypatch, "shadow")

    reader = FakeFirestoreReader(
        [_doc("customers/c1", display_name="Acme", updated_at="2026-06-02T00:00:00Z")]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader, store=store)
    obj = await store.get_object_by_source("firestore", "customers/c1", workspace_id=ws)

    await store.update_object(
        obj.id, {"name": "Acme Global"}, workspace_id=ws, writer_class="agent"
    )

    stmts = await store.get_statements(obj.id, "name", workspace_id=ws)
    assert {s.value for s in stmts} == {"Acme", "Acme Global"}  # BOTH claims present


# --------------------------------------------------------------------------
# mode=off — the mirror is byte-for-byte untouched.
# --------------------------------------------------------------------------


async def test_mode_off_mirror_ingest_writes_no_statements(
    mongo_db,
    tmp_path,
    monkeypatch,  # noqa: ARG001
):
    store = await _make_store(tmp_path)
    ws = "w1"
    await _seed_config(ws, [_customer_mapping()])
    _set_mode(monkeypatch, "off")

    reader1 = FakeFirestoreReader(
        [_doc("customers/c1", display_name="Acme", updated_at="2026-06-02T00:00:00Z")]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader1, store=store)
    reader2 = FakeFirestoreReader(
        [_doc("customers/c1", display_name="Acme v2", updated_at="2026-06-04T00:00:00Z")]
    )
    result = await ingest_service.ingest_collection(ws, "customers", reader=reader2, store=store)

    assert result["status"] == "ok"
    obj = await store.get_object_by_source("firestore", "customers/c1", workspace_id=ws)
    assert obj.properties["name"] == "Acme v2"  # the mirror still mirrors
    assert _table_count(store, "fabric_statements") == 0
    assert _table_count(store, "fabric_sources") == 0
