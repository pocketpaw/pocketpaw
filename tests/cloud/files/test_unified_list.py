# tests/cloud/files/test_unified_list.py — Coverage for the unified
# files service + mongo_store listing added in Cluster E sub-PR 4.
# Exercises workspace scope, dedupe, drive-stub warning, and limit cap.
# Created: 2026-04-19

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient
from pocketpaw_ee.cloud.files.service import UnifiedFile, UnifiedFilesService, _dedupe
from pocketpaw_ee.cloud.uploads.models import FileUpload
from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

from pocketpaw.uploads.file_store import FileRecord


@pytest.fixture()
async def beanie_files_db():
    db_name = f"test_files_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]
    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]

    await init_beanie(database=db, document_models=[FileUpload])
    yield db


async def _seed_upload(workspace: str, *, name: str, chat_id: str | None = None) -> None:
    store = MongoFileStore()
    rec = FileRecord(
        id=uuid.uuid4().hex,
        storage_key=f"keys/{uuid.uuid4().hex}",
        filename=name,
        mime="text/plain",
        size=123,
        owner_id="u1",
        chat_id=chat_id,
        created=datetime.now(),
    )
    await store.save_scoped(rec, workspace=workspace)


@pytest.mark.asyncio
async def test_list_by_workspace_returns_rows(beanie_files_db):
    await _seed_upload("w1", name="a.pdf")
    await _seed_upload("w1", name="b.pdf")

    store = MongoFileStore()
    rows = await store.list_by_workspace("w1", limit=50)

    assert {r.filename for r in rows} == {"a.pdf", "b.pdf"}


@pytest.mark.asyncio
async def test_list_by_workspace_skips_other_workspaces(beanie_files_db):
    """Cross-workspace isolation — a file in workspace A never surfaces
    through a listing against workspace B, even for the same caller."""
    await _seed_upload("w-a", name="A-file.pdf")
    await _seed_upload("w-b", name="B-file.pdf")

    store = MongoFileStore()
    rows = await store.list_by_workspace("w-a", limit=50)

    assert [r.filename for r in rows] == ["A-file.pdf"]


@pytest.mark.asyncio
async def test_list_by_workspace_soft_delete_is_hidden(beanie_files_db):
    await _seed_upload("w1", name="keep.pdf")
    await _seed_upload("w1", name="gone.pdf")
    # Soft-delete one row directly on the doc for speed.
    doc = await FileUpload.find_one(FileUpload.filename == "gone.pdf")
    assert doc is not None
    doc.deleted_at = datetime.now()
    await doc.save()

    rows = await MongoFileStore().list_by_workspace("w1", limit=50)
    assert [r.filename for r in rows] == ["keep.pdf"]


@pytest.mark.asyncio
async def test_unified_list_includes_chat_and_drive_warning(beanie_files_db):
    await _seed_upload("w1", name="chat-attachment.pdf")

    svc = UnifiedFilesService()
    page = await svc.list_unified("w1", source=None, limit=50)

    assert [f.source for f in page.files] == ["chat"]
    assert [f.filename for f in page.files] == ["chat-attachment.pdf"]
    # Drive branch is stubbed — the service flags it so the FE can
    # render a "connect Drive" nudge without guessing.
    assert any("drive.not_connected" in w for w in page.warnings)


@pytest.mark.asyncio
async def test_unified_list_chat_only_has_no_drive_warning(beanie_files_db):
    await _seed_upload("w1", name="a.pdf")

    svc = UnifiedFilesService()
    page = await svc.list_unified("w1", source="chat", limit=50)

    assert len(page.files) == 1
    assert page.warnings == []


@pytest.mark.asyncio
async def test_unified_list_local_source_warns_client_only(beanie_files_db):
    svc = UnifiedFilesService()
    page = await svc.list_unified("w1", source="local", limit=50)

    assert page.files == []
    assert any("local.client_only" in w for w in page.warnings)


def test_dedupe_keeps_first_occurrence():
    now = datetime.now()
    rows = [
        UnifiedFile(
            id="a", source="chat", filename="a.pdf", mime="x", size=100, url="u", created=now
        ),
        UnifiedFile(
            id="b", source="drive", filename="a.pdf", mime="x", size=100, url="u2", created=now
        ),
        UnifiedFile(
            id="c", source="chat", filename="b.pdf", mime="x", size=200, url="u", created=now
        ),
    ]

    out = _dedupe(rows)
    assert [r.id for r in out] == ["a", "c"]


@pytest.mark.asyncio
async def test_unified_list_respects_limit_cap(beanie_files_db):
    for i in range(6):
        await _seed_upload("w1", name=f"f{i}.pdf")

    svc = UnifiedFilesService()
    page = await svc.list_unified("w1", source="chat", limit=3)
    assert len(page.files) == 3


@pytest.mark.asyncio
async def test_unified_list_offset_pagination(beanie_files_db):
    """Offset paging walks the newest-first merged set and reports an
    accurate total + has_more (total comes from a count, not the page)."""
    for i in range(5):
        await _seed_upload("w1", name=f"f{i}.pdf")

    svc = UnifiedFilesService()

    first = await svc.list_unified("w1", source="chat", limit=2, offset=0)
    assert len(first.files) == 2
    assert first.total == 5
    assert first.has_more is True

    second = await svc.list_unified("w1", source="chat", limit=2, offset=2)
    assert len(second.files) == 2
    assert second.total == 5
    assert second.has_more is True

    last = await svc.list_unified("w1", source="chat", limit=2, offset=4)
    assert len(last.files) == 1
    assert last.total == 5
    assert last.has_more is False

    # Pages don't overlap and cover every row exactly once (newest first).
    names = [f.filename for f in first.files + second.files + last.files]
    assert sorted(names) == [f"f{i}.pdf" for i in range(5)]


@pytest.mark.asyncio
async def test_unified_list_has_more_false_on_short_page(beanie_files_db):
    """A page short of ``limit`` on the last batch never claims more rows."""
    for i in range(3):
        await _seed_upload("w1", name=f"f{i}.pdf")

    svc = UnifiedFilesService()
    page = await svc.list_unified("w1", source="chat", limit=10, offset=0)
    assert len(page.files) == 3
    assert page.total == 3
    assert page.has_more is False
