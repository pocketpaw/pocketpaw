# tests/cloud/fabric_ingest/test_fabric_ingest_enforce.py
# Created: 2026-07-10 (FST-5 — ENFORCE at merge site 3, the Firestore mirror).
#
# Site 3 needed NO code change for enforce: the mirror's update path delegates
# to store.update_object (via the canonical OSS ingest loop), so the FST-5
# enforce cache semantics flow through AUTOMATICALLY. This file is the proof:
#
#   * a mirror re-sync of a TRACKED property lands in the cache as the
#     RESOLVER'S winner (the connector-tier baseline beats the mirror tier on
#     the trust ladder), not the mirror's blind LWW value — while the mirror's
#     claim is still recorded as a statement,
#   * a mirror self-refresh of UNTRACKED properties keeps plain LWW even in
#     enforce (writer-family rule: no promotion, nothing to resolve — the
#     mirror still mirrors).
#
# Ruling documented in service.py's header: the within-source field-map
# collapse in _mirror_docs (two Firestore fields mapping to one property →
# last mapping wins) STAYS — it happens before the store write and is a
# mapping-configuration concern within ONE source, not a trust conflict.
#
# Same harness as test_fabric_ingest_shadow.py: fake reader, real FabricStore
# on tmp SQLite, config in the mongo_db fixture — no google creds.

from __future__ import annotations

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


# --------------------------------------------------------------------------
# Enforce flows through the mirror's update path automatically.
# --------------------------------------------------------------------------


async def test_enforce_flows_through_mirror_resync_tracked_property(
    mongo_db,
    tmp_path,
    monkeypatch,  # noqa: ARG001
):
    store = await _make_store(tmp_path)
    ws = "w1"
    await _seed_config(ws, [_customer_mapping()])
    _set_mode(monkeypatch, "enforce")

    # Backfill mirrors the doc (CREATE — no statements, the FST-4 ruling).
    reader1 = FakeFirestoreReader(
        [_doc("customers/c1", display_name="Acme", tier="gold", updated_at="2026-06-02T10:00:00Z")]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader1, store=store)
    obj = await store.get_object_by_source("firestore", "customers/c1", workspace_id=ws)
    assert obj is not None

    # An agent edit promotes "name". In enforce the store already resolves:
    # the connector-tier baseline seed ("Acme") beats the agent claim, so the
    # cache keeps "Acme".
    await store.update_object(
        obj.id,
        {"name": "Acme (agent edit)"},
        workspace_id=ws,
        writer_class="agent",
        source_session_id="sess-1",
    )
    tracked = await store.get_object(obj.id, workspace_id=ws)
    assert tracked is not None and tracked.properties["name"] == "Acme"

    # The mirror re-syncs a newer doc version THROUGH ingest_collection —
    # merge site 3, no enforce-specific code anywhere in the worker.
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

    # The mirror's claim IS recorded...
    stmts = await store.get_statements(obj.id, "name", workspace_id=ws)
    assert len(stmts) == 3
    mirror = next(s for s in stmts if s.writer_class == "mirror")
    assert mirror.value == "Acme v2"

    # ...but the cache holds the RESOLVER'S winner (the connector-tier seed
    # outranks mirror on the ladder), not the mirror's LWW value. Enforce
    # flowed through store.update_object automatically.
    refreshed = await store.get_object(obj.id, workspace_id=ws)
    assert refreshed is not None and refreshed.properties["name"] == "Acme"


async def test_enforce_untracked_mirror_refresh_keeps_lww(
    mongo_db,
    tmp_path,
    monkeypatch,  # noqa: ARG001
):
    """A mirror self-refresh promotes nothing (writer-family rule), so its
    untracked properties have no statements and enforce has nothing to
    resolve: plain LWW — the mirror still mirrors, byte-for-byte."""
    store = await _make_store(tmp_path)
    ws = "w1"
    await _seed_config(ws, [_customer_mapping()])
    _set_mode(monkeypatch, "enforce")

    reader1 = FakeFirestoreReader(
        [_doc("customers/c1", display_name="Acme", tier="gold", updated_at="2026-06-02T00:00:00Z")]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader1, store=store)
    reader2 = FakeFirestoreReader(
        [
            _doc(
                "customers/c1",
                display_name="Acme Renamed",
                tier="silver",
                updated_at="2026-06-04T00:00:00Z",
            )
        ]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader2, store=store)

    obj = await store.get_object_by_source("firestore", "customers/c1", workspace_id=ws)
    assert obj is not None
    assert obj.properties["name"] == "Acme Renamed"  # LWW refresh intact
    assert obj.properties["tier"] == "silver"
    assert await store.get_statements(obj.id, "name", workspace_id=ws) == []
