# test_pocket_isolation.py — pocket-scoped file isolation tests.
# Created: 2026-05-03 — Stage 3.E "Files as Knowledge". Verifies that
# a pocket's files surface only to that pocket and that workspace
# listings never see them. Listener-side scope routing is covered in
# test_listener_pocket_route.py.
"""Pocket isolation: files in pocket A never leak to pocket B or workspace."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from pocketpaw_ee.cloud.files.service import UnifiedFilesService
from pocketpaw_ee.cloud.uploads.mongo_store import LIST_WORKSPACE_ONLY, MongoFileStore

from pocketpaw.uploads.file_store import FileRecord


async def _seed(
    workspace: str,
    *,
    name: str,
    pocket_id: str | None = None,
    chat_id: str | None = None,
) -> str:
    store = MongoFileStore()
    rec = FileRecord(
        id=uuid.uuid4().hex,
        storage_key=f"keys/{uuid.uuid4().hex}",
        filename=name,
        mime="text/plain",
        size=11,
        owner_id="u1",
        chat_id=chat_id,
        created=datetime.now(),
    )
    await store.save_scoped(rec, workspace=workspace, pocket_id=pocket_id)
    return rec.id


@pytest.mark.asyncio
async def test_pocket_a_listing_returns_only_pocket_a_files(beanie_upload_db):
    """Listing pocket A returns A's files; B's files never surface."""
    await _seed("w1", name="a-secret.pdf", pocket_id="A")
    await _seed("w1", name="b-secret.pdf", pocket_id="B")

    svc = UnifiedFilesService()
    files, _warnings = await svc.list_unified("w1", source="chat", limit=50, pocket_id="A")

    assert [f.filename for f in files] == ["a-secret.pdf"]


@pytest.mark.asyncio
async def test_pocket_b_listing_returns_only_pocket_b_files(beanie_upload_db):
    await _seed("w1", name="a-secret.pdf", pocket_id="A")
    await _seed("w1", name="b-secret.pdf", pocket_id="B")

    svc = UnifiedFilesService()
    files, _warnings = await svc.list_unified("w1", source="chat", limit=50, pocket_id="B")

    assert [f.filename for f in files] == ["b-secret.pdf"]


@pytest.mark.asyncio
async def test_workspace_listing_excludes_all_pocket_files(beanie_upload_db):
    """No ``pocket_id`` → workspace listing never sees pocket files.

    The privacy contract: pocket files don't bleed into the workspace
    Files panel even though they all share one Mongo collection. Filter
    is metadata-only (Captain Option A).
    """
    await _seed("w1", name="ws-doc.pdf")  # workspace-scoped
    await _seed("w1", name="a-secret.pdf", pocket_id="A")
    await _seed("w1", name="b-secret.pdf", pocket_id="B")

    svc = UnifiedFilesService()
    files, _warnings = await svc.list_unified("w1", source="chat", limit=50, pocket_id=None)

    assert [f.filename for f in files] == ["ws-doc.pdf"]


@pytest.mark.asyncio
async def test_cross_workspace_isolation_holds_within_pocket_filter(
    beanie_upload_db,
):
    """Two workspaces sharing a pocket id (impossible in practice but
    defensive): the workspace filter still wins."""
    await _seed("w1", name="w1-pa.pdf", pocket_id="P")
    await _seed("w2", name="w2-pa.pdf", pocket_id="P")

    svc = UnifiedFilesService()
    files, _ = await svc.list_unified("w1", source="chat", limit=50, pocket_id="P")

    assert [f.filename for f in files] == ["w1-pa.pdf"]


@pytest.mark.asyncio
async def test_iter_by_pocket_filters_to_one_pocket(beanie_upload_db):
    """``MongoFileStore.iter_by_pocket`` is the symmetric helper used by
    the unified files panel when a pocket-scoped listing is requested."""
    await _seed("w1", name="a1.pdf", pocket_id="A")
    await _seed("w1", name="a2.pdf", pocket_id="A")
    await _seed("w1", name="b1.pdf", pocket_id="B")
    await _seed("w1", name="ws.pdf")  # workspace-scoped

    store = MongoFileStore()
    names: list[str] = []
    async for row in store.iter_by_pocket("w1", "A"):
        names.append(row["filename"])

    assert sorted(names) == ["a1.pdf", "a2.pdf"]


@pytest.mark.asyncio
async def test_count_by_pocket_returns_live_count(beanie_upload_db):
    await _seed("w1", name="a1.pdf", pocket_id="A")
    await _seed("w1", name="a2.pdf", pocket_id="A")
    await _seed("w1", name="b1.pdf", pocket_id="B")

    store = MongoFileStore()
    assert await store.count_by_pocket("w1", "A") == 2
    assert await store.count_by_pocket("w1", "B") == 1
    assert await store.count_by_pocket("w1", "missing") == 0


@pytest.mark.asyncio
async def test_list_by_workspace_workspace_only_sentinel(beanie_upload_db):
    """``LIST_WORKSPACE_ONLY`` filters to ``pocket_id IS None`` rows."""
    await _seed("w1", name="ws.pdf")  # pocket_id=None
    await _seed("w1", name="pa.pdf", pocket_id="A")

    store = MongoFileStore()
    rows = await store.list_by_workspace("w1", limit=50, pocket_id=LIST_WORKSPACE_ONLY)
    assert [r.filename for r in rows] == ["ws.pdf"]


@pytest.mark.asyncio
async def test_list_by_workspace_pocket_id_string_filters(beanie_upload_db):
    """Plain string filter narrows to that pocket's rows."""
    await _seed("w1", name="ws.pdf")
    await _seed("w1", name="pa.pdf", pocket_id="A")
    await _seed("w1", name="pb.pdf", pocket_id="B")

    store = MongoFileStore()
    rows = await store.list_by_workspace("w1", limit=50, pocket_id="A")
    assert [r.filename for r in rows] == ["pa.pdf"]


@pytest.mark.asyncio
async def test_list_by_workspace_default_returns_everything(beanie_upload_db):
    """``pocket_id=None`` (default) keeps the legacy "no filter" behaviour
    so callers that never pass the kwarg keep working as before. The
    UnifiedFilesService chat-uploads slice always passes a sentinel or
    a pocket id; the bare default is for back-compat."""
    await _seed("w1", name="ws.pdf")
    await _seed("w1", name="pa.pdf", pocket_id="A")

    store = MongoFileStore()
    rows = await store.list_by_workspace("w1", limit=50)
    names = sorted(r.filename for r in rows)
    assert names == ["pa.pdf", "ws.pdf"]
