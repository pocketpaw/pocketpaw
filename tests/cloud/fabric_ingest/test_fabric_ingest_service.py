# tests/cloud/fabric_ingest/test_fabric_ingest_service.py
# Created: 2026-06-11 — generic Firestore→Fabric ingestion worker.
#
# Pins the per-collection Firestore→Fabric ingest contract. The worker is fully
# generic: the mapping (collection, object type, field map, cursor field) comes
# from a per-workspace FabricIngestConfig seeded in each test — no domain
# specifics in the code under test. Coverage:
#
#   1. happy path — a fake reader's docs land as Fabric objects, stamped with
#      workspace_id + source_connector="firestore" + source_id (doc path), and
#      the field map projects the right properties.
#   2. cursor advances to the MAX doc cursor-field value, NOT run wall-clock.
#   3. re-run upserts, not duplicates — the same source_id updates in place.
#   4. workspace_id is stamped on EVERY created object.
#   5. config DTO validation rejects a bad mapping.
#   6. (sweep file) one collection failing doesn't sink the others.
#
# Firestore access is a fake reader and the write sink is a real FabricStore on
# a tmp SQLite file, so the suite runs with no google creds and no network.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.fabric_ingest import service as ingest_service  # noqa: E402
from pocketpaw_ee.cloud.models.fabric_ingest_state import (  # noqa: E402
    FabricFieldMapping,
    FabricIngestConfig,
    FabricIngestState,
    FabricLinkRule,
)

from pocketpaw.fabric.models import PropertyDef  # noqa: E402
from pocketpaw.fabric.store import FabricStore  # noqa: E402

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fakes + helpers.
# --------------------------------------------------------------------------


class FakeFirestoreReader:
    """Stands in for the google-cloud-firestore client.

    Records the (collection, cursor_field, cursor, limit) it was asked for so a
    test can assert the backfill-vs-incremental window, and returns only the
    documents whose cursor value is > the supplied cursor (mimicking the real
    reader's start_after semantics)."""

    def __init__(self, docs: list[dict] | None = None) -> None:
        self._docs = docs or []
        self.calls: list[tuple[str, str, str, int]] = []

    async def read_collection(self, collection, *, cursor_field, cursor, limit):
        self.calls.append((collection, cursor_field, cursor, limit))
        out = []
        for d in self._docs:
            val = (d.get("data") or {}).get(cursor_field)
            if val is None or val == "":
                val = d.get("update_time") or ""
            if cursor and str(val) <= cursor:
                continue  # already past the high-water mark
            out.append(d)
        return out[:limit]


def _doc(path: str, **fields) -> dict:
    """A Firestore document in the worker's wire shape."""
    update_time = fields.pop("_update_time", "2026-06-01T00:00:00Z")
    return {"path": path, "data": dict(fields), "update_time": update_time}


async def _make_store(tmp_path) -> FabricStore:
    """A real FabricStore on a tmp SQLite file with one object type defined."""
    store = FabricStore(tmp_path / "fabric.db")
    await store.define_type("Customer", [PropertyDef(name="name")])
    return store


async def _seed_config(workspace_id: str, mappings: list[FabricFieldMapping]) -> None:
    cfg = FabricIngestConfig(workspace=workspace_id, enabled=True, mappings=mappings)
    await cfg.insert()


# --------------------------------------------------------------------------
# 1 — happy path: docs land as objects with tenancy + provenance + properties.
# --------------------------------------------------------------------------


async def test_ingest_happy_path_creates_stamped_objects(mongo_db, tmp_path):  # noqa: ARG001
    store = await _make_store(tmp_path)
    ws = "w1"
    mapping = FabricFieldMapping(
        collection="customers",
        object_type_id="ot-customer",
        field_map={"display_name": "name", "tier": "tier"},
        cursor_field="updated_at",
    )
    await _seed_config(ws, [mapping])

    reader = FakeFirestoreReader(
        [
            _doc(
                "customers/c1", display_name="Acme", tier="gold", updated_at="2026-06-02T10:00:00Z"
            ),
            _doc(
                "customers/c2",
                display_name="Globex",
                tier="silver",
                updated_at="2026-06-03T10:00:00Z",
            ),
        ]
    )

    result = await ingest_service.ingest_collection(ws, "customers", reader=reader, store=store)

    assert result["status"] == "ok"
    assert result["mode"] == "backfill"
    assert result["objects"] == 2

    # Both objects exist, scoped to the workspace, with the mapped properties.
    obj1 = await store.get_object_by_source("firestore", "customers/c1", workspace_id=ws)
    obj2 = await store.get_object_by_source("firestore", "customers/c2", workspace_id=ws)
    assert obj1 is not None and obj2 is not None
    assert obj1.source_connector == "firestore"
    assert obj1.source_id == "customers/c1"
    assert obj1.properties == {"name": "Acme", "tier": "gold"}
    assert obj2.properties == {"name": "Globex", "tier": "silver"}


# --------------------------------------------------------------------------
# 2 — cursor advances to the MAX doc cursor-field value, not run wall-clock.
# --------------------------------------------------------------------------


async def test_cursor_advances_to_max_doc_cursor_field_not_wallclock(mongo_db, tmp_path):  # noqa: ARG001
    store = await _make_store(tmp_path)
    ws = "w1"
    await _seed_config(
        ws,
        [
            FabricFieldMapping(
                collection="customers",
                object_type_id="ot-customer",
                field_map={"display_name": "name"},
                cursor_field="updated_at",
            )
        ],
    )
    # Docs deliberately out of order; the newest cursor value is the middle doc.
    reader = FakeFirestoreReader(
        [
            _doc("customers/c1", display_name="A", updated_at="2026-06-02T00:00:00Z"),
            _doc("customers/c3", display_name="C", updated_at="2026-06-09T00:00:00Z"),
            _doc("customers/c2", display_name="B", updated_at="2026-06-05T00:00:00Z"),
        ]
    )

    result = await ingest_service.ingest_collection(ws, "customers", reader=reader, store=store)

    # The cursor is the MAX cursor_field value seen — a real watermark from the
    # document data — not a wall-clock "now" (which would be 2026-06-11+).
    assert result["cursor"] == "2026-06-09T00:00:00Z"
    state = await FabricIngestState.find_one(
        FabricIngestState.workspace == ws,
        FabricIngestState.source_id == "customers",
    )
    assert state.cursor == "2026-06-09T00:00:00Z"

    # A second (incremental) run must read with that watermark as the lower
    # bound, so an unchanged doc below it is never re-fetched.
    reader2 = FakeFirestoreReader(
        [
            _doc("customers/c1", display_name="A", updated_at="2026-06-02T00:00:00Z"),  # stale
            _doc("customers/c4", display_name="D", updated_at="2026-06-10T00:00:00Z"),  # new
        ]
    )
    result2 = await ingest_service.ingest_collection(ws, "customers", reader=reader2, store=store)
    assert result2["mode"] == "incremental"
    # The reader was called with the stored watermark as the cursor.
    _coll, _field, used_cursor, _limit = reader2.calls[0]
    assert used_cursor == "2026-06-09T00:00:00Z"
    # Only the new doc came through; the stale one was filtered by the bound.
    assert result2["objects"] == 1
    assert result2["cursor"] == "2026-06-10T00:00:00Z"


# --------------------------------------------------------------------------
# 3 — re-run upserts (same source_id), no duplicates.
# --------------------------------------------------------------------------


async def test_rerun_upserts_same_source_id_no_duplicates(mongo_db, tmp_path):  # noqa: ARG001
    store = await _make_store(tmp_path)
    ws = "w1"
    await _seed_config(
        ws,
        [
            FabricFieldMapping(
                collection="customers",
                object_type_id="ot-customer",
                field_map={"display_name": "name"},
                cursor_field="updated_at",
            )
        ],
    )

    # First run mirrors c1.
    reader1 = FakeFirestoreReader(
        [_doc("customers/c1", display_name="Acme", updated_at="2026-06-02T00:00:00Z")]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader1, store=store)
    obj_first = await store.get_object_by_source("firestore", "customers/c1", workspace_id=ws)

    # Backfill again from scratch (delete state) with an UPDATED c1 — same path.
    state = await FabricIngestState.find_one(
        FabricIngestState.workspace == ws, FabricIngestState.source_id == "customers"
    )
    await state.delete()  # force a fresh backfill so the same doc is re-read
    reader2 = FakeFirestoreReader(
        [_doc("customers/c1", display_name="Acme Renamed", updated_at="2026-06-04T00:00:00Z")]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader2, store=store)

    # Still exactly ONE object for that source path — updated, not duplicated.
    from pocketpaw.fabric.models import FabricQuery

    res = await store.query(FabricQuery(type_id="ot-customer"), workspace_id=ws)
    matching = [o for o in res.objects if o.source_id == "customers/c1"]
    assert len(matching) == 1
    # And it was updated in place (same object id, new property value).
    obj_second = await store.get_object_by_source("firestore", "customers/c1", workspace_id=ws)
    assert obj_second.id == obj_first.id
    assert obj_second.properties["name"] == "Acme Renamed"


# --------------------------------------------------------------------------
# 4 — workspace_id stamped on EVERY created object (tenancy leak otherwise).
# --------------------------------------------------------------------------


async def test_workspace_id_stamped_on_every_object(mongo_db, tmp_path):  # noqa: ARG001
    store = await _make_store(tmp_path)
    ws = "w-tenant-a"
    await _seed_config(
        ws,
        [
            FabricFieldMapping(
                collection="customers",
                object_type_id="ot-customer",
                field_map={"display_name": "name"},
                cursor_field="updated_at",
            )
        ],
    )
    reader = FakeFirestoreReader(
        [
            _doc("customers/c1", display_name="A", updated_at="2026-06-02T00:00:00Z"),
            _doc("customers/c2", display_name="B", updated_at="2026-06-03T00:00:00Z"),
        ]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader, store=store)

    # A read scoped to a DIFFERENT workspace must NOT see these objects (they
    # carry ws-tenant-a, not NULL — so they don't leak to another tenant).
    from pocketpaw.fabric.models import FabricQuery

    other = await store.query(FabricQuery(type_id="ot-customer"), workspace_id="w-tenant-b")
    leaked = [o for o in other.objects if o.source_id in {"customers/c1", "customers/c2"}]
    assert leaked == []
    # The owning tenant sees both.
    owned = await store.query(FabricQuery(type_id="ot-customer"), workspace_id=ws)
    assert {o.source_id for o in owned.objects} >= {"customers/c1", "customers/c2"}


# --------------------------------------------------------------------------
# 5 — config DTO validation rejects a bad mapping.
# --------------------------------------------------------------------------


async def test_config_dto_rejects_bad_mapping():
    # Pure-validation test (no I/O); async only because the module is
    # asyncio-marked, so pytest-asyncio runs it without a stray-mark warning.
    from pocketpaw_ee.cloud.fabric_ingest.dto import IngestConfigRequest

    # Blank object_type_id — there is nowhere to put the mirrored data.
    with pytest.raises(Exception):
        IngestConfigRequest.model_validate(
            {
                "workspace_id": "w1",
                "mappings": [{"collection": "customers", "object_type_id": ""}],
            }
        )

    # Blank collection — nothing to read.
    with pytest.raises(Exception):
        IngestConfigRequest.model_validate(
            {
                "workspace_id": "w1",
                "mappings": [{"collection": "  ", "object_type_id": "ot-1"}],
            }
        )

    # A link rule missing via_field is rejected.
    with pytest.raises(Exception):
        IngestConfigRequest.model_validate(
            {
                "workspace_id": "w1",
                "mappings": [
                    {
                        "collection": "customers",
                        "object_type_id": "ot-1",
                        "link_rules": [{"to_type": "ot-2", "link_type": "belongs_to"}],
                    }
                ],
            }
        )

    # Blank workspace_id is rejected.
    with pytest.raises(Exception):
        IngestConfigRequest.model_validate({"workspace_id": "  ", "mappings": []})

    # A well-formed config validates cleanly.
    ok = IngestConfigRequest.model_validate(
        {
            "workspace_id": "w1",
            "mappings": [
                {
                    "collection": "customers",
                    "object_type_id": "ot-1",
                    "field_map": {"display_name": "name"},
                    "cursor_field": "updated_at",
                    "link_rules": [
                        {"to_type": "ot-2", "link_type": "belongs_to", "via_field": "account_ref"}
                    ],
                }
            ],
        }
    )
    assert ok.mappings[0].collection == "customers"


# --------------------------------------------------------------------------
# Error isolation — a read failure marks error, writes nothing, doesn't crash.
# --------------------------------------------------------------------------


async def test_read_failure_marks_error_and_writes_nothing(mongo_db, tmp_path):  # noqa: ARG001
    store = await _make_store(tmp_path)
    ws = "w1"
    await _seed_config(
        ws,
        [
            FabricFieldMapping(
                collection="customers",
                object_type_id="ot-customer",
                field_map={"display_name": "name"},
                cursor_field="updated_at",
            )
        ],
    )

    class BoomReader:
        async def read_collection(self, *_a, **_k):
            raise RuntimeError("firestore permission denied")

    result = await ingest_service.ingest_collection(
        ws, "customers", reader=BoomReader(), store=store
    )

    assert result["status"] == "error"
    assert result["objects"] == 0
    assert result["errors"]
    # Nothing was written.
    from pocketpaw.fabric.models import FabricQuery

    res = await store.query(FabricQuery(type_id="ot-customer"), workspace_id=ws)
    assert res.total == 0
    # Backfill is NOT marked done on a failed run, so it retries the wide read.
    state = await FabricIngestState.find_one(
        FabricIngestState.workspace == ws, FabricIngestState.source_id == "customers"
    )
    assert state.status == "error"
    assert state.backfill_done is False
    assert state.cursor == ""


async def test_unmapped_collection_is_error_not_crash(mongo_db, tmp_path):  # noqa: ARG001
    store = await _make_store(tmp_path)
    ws = "w1"
    # Config has a mapping for "customers" only; ask for "orders".
    await _seed_config(
        ws,
        [FabricFieldMapping(collection="customers", object_type_id="ot-customer")],
    )
    result = await ingest_service.ingest_collection(
        ws, "orders", reader=FakeFirestoreReader([]), store=store
    )
    assert result["status"] == "error"
    assert "no mapping" in result["errors"][0]


# --------------------------------------------------------------------------
# Bonus — link rules wire objects together by source path.
# --------------------------------------------------------------------------


async def test_link_rules_link_by_source_path(mongo_db, tmp_path):  # noqa: ARG001
    store = FabricStore(tmp_path / "fabric.db")
    await store.define_type("Account", [PropertyDef(name="name")])
    await store.define_type("Customer", [PropertyDef(name="name")])
    ws = "w1"

    # Seed an Account object first (the link target).
    await store.create_object(
        type_id="ot-account",
        properties={"name": "Acme Corp"},
        source_connector="firestore",
        source_id="accounts/a1",
        workspace_id=ws,
    )

    await _seed_config(
        ws,
        [
            FabricFieldMapping(
                collection="customers",
                object_type_id="ot-customer",
                field_map={"display_name": "name"},
                cursor_field="updated_at",
                link_rules=[
                    FabricLinkRule(
                        to_type="ot-account", link_type="belongs_to", via_field="account_ref"
                    )
                ],
            )
        ],
    )

    reader = FakeFirestoreReader(
        [
            _doc(
                "customers/c1",
                display_name="Acme",
                account_ref="accounts/a1",
                updated_at="2026-06-02T00:00:00Z",
            )
        ]
    )
    await ingest_service.ingest_collection(ws, "customers", reader=reader, store=store)

    cust = await store.get_object_by_source("firestore", "customers/c1", workspace_id=ws)
    acct = await store.get_object_by_source("firestore", "accounts/a1", workspace_id=ws)
    links, total = await store.list_links(from_id=cust.id, workspace_id=ws)
    assert total == 1
    assert links[0].to_object_id == acct.id
    assert links[0].link_type == "belongs_to"
