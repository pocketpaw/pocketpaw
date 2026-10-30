# tests/cloud/uploads/test_mongo_store.py
# 2026-07-03 (FL-1 "Library metadata"): added TestLibraryMetadata covering
# set_library_metadata (set tags/collections/hide_from_ai + partial update),
# the default-on-missing read path for legacy rows, and that iter_by_workspace
# surfaces the three fields. The pre-existing TestMongoFileStore cases are
# unchanged.
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.asyncio


def _record(**overrides) -> FileRecord:
    defaults = {
        "id": "f1",
        "storage_key": "chat/202604/aaa.png",
        "filename": "cat.png",
        "mime": "image/png",
        "size": 1,
        "owner_id": "u1",
        "chat_id": "c1",
        "created": datetime.now(UTC),
    }
    defaults.update(overrides)
    return FileRecord(**defaults)


class TestMongoFileStore:
    async def test_save_then_get(self, store):
        await store.save_scoped(_record(), workspace="w1")
        got = await store.get_scoped("f1", workspace="w1")
        assert got is not None
        assert got.filename == "cat.png"

    async def test_cross_workspace_get_returns_none(self, store):
        await store.save_scoped(_record(), workspace="w1")
        assert await store.get_scoped("f1", workspace="w2") is None

    async def test_soft_delete_hides(self, store):
        await store.save_scoped(_record(), workspace="w1")
        await store.soft_delete_scoped("f1", workspace="w1")
        assert await store.get_scoped("f1", workspace="w1") is None

    async def test_get_missing_returns_none(self, store):
        assert await store.get_scoped("nope", workspace="w1") is None


class TestLibraryMetadata:
    """FL-1: tags / collections / hide_from_ai set, read, and default."""

    async def test_defaults_on_freshly_saved_row(self, store):
        # A row saved through the normal path (no library metadata) reads back
        # with empty lists / False — this is the default-on-missing path.
        await store.save_scoped(_record(), workspace="w1")
        doc = await store.get_doc_scoped("f1", workspace="w1")
        assert doc is not None
        assert doc.tags == []
        assert doc.collections == []
        assert doc.hide_from_ai is False

    async def test_legacy_row_without_fields_reads_as_empty(self, store):
        # Simulate a legacy row that predates FL-1: insert the doc, then strip
        # the new keys straight out of Mongo so the fields are truly absent.
        from pocketpaw_ee.cloud.uploads.models import FileUpload

        await store.save_scoped(_record(), workspace="w1")
        coll = FileUpload.get_pymongo_collection()
        await coll.update_one(
            {"file_id": "f1"},
            {"$unset": {"tags": "", "collections": "", "hide_from_ai": ""}},
        )
        doc = await store.get_doc_scoped("f1", workspace="w1")
        assert doc is not None
        assert doc.tags == []
        assert doc.collections == []
        assert doc.hide_from_ai is False

    async def test_set_library_metadata_roundtrips(self, store):
        await store.save_scoped(_record(), workspace="w1")
        updated = await store.set_library_metadata(
            "f1",
            "w1",
            tags=["invoice", "2026"],
            collections=["Q3"],
            hide_from_ai=True,
        )
        assert updated is not None
        doc = await store.get_doc_scoped("f1", workspace="w1")
        assert doc is not None
        assert doc.tags == ["invoice", "2026"]
        assert doc.collections == ["Q3"]
        assert doc.hide_from_ai is True

    async def test_set_library_metadata_partial_update(self, store):
        await store.save_scoped(_record(), workspace="w1")
        await store.set_library_metadata("f1", "w1", tags=["keep"])
        # Only hide_from_ai changes now; tags must be preserved.
        await store.set_library_metadata("f1", "w1", hide_from_ai=True)
        doc = await store.get_doc_scoped("f1", workspace="w1")
        assert doc is not None
        assert doc.tags == ["keep"]
        assert doc.hide_from_ai is True

    async def test_set_library_metadata_missing_returns_none(self, store):
        assert await store.set_library_metadata("nope", "w1", tags=["x"]) is None

    async def test_set_library_metadata_is_workspace_scoped(self, store):
        await store.save_scoped(_record(), workspace="w1")
        # Another workspace cannot touch the row.
        assert await store.set_library_metadata("f1", "w2", tags=["x"]) is None
        doc = await store.get_doc_scoped("f1", workspace="w1")
        assert doc is not None and doc.tags == []

    async def test_iter_by_workspace_surfaces_fields(self, store):
        await store.save_scoped(_record(), workspace="w1")
        await store.set_library_metadata(
            "f1", "w1", tags=["a"], collections=["c"], hide_from_ai=True
        )
        rows = [r async for r in store.iter_by_workspace("w1")]
        assert len(rows) == 1
        assert rows[0]["tags"] == ["a"]
        assert rows[0]["collections"] == ["c"]
        assert rows[0]["hide_from_ai"] is True
