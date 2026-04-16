"""Tests for ee.cloud.uploads.resolver."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from ee.cloud.uploads.resolver import EEUploadResolver, resolve_media_paths_scoped
from pocketpaw.uploads.file_store import FileRecord
from pocketpaw.uploads.local import LocalStorageAdapter

pytestmark = pytest.mark.asyncio


async def _stash(
    tmp_upload_root: Path,
    store,  # MongoFileStore
    workspace: str,
    data: bytes = b"hello",
    filename: str = "x.txt",
    mime: str = "text/plain",
) -> tuple[FileRecord, Path]:
    from datetime import UTC, datetime

    file_id = uuid.uuid4().hex
    storage_key = f"chat/202604/{file_id}.txt"
    disk_path = tmp_upload_root / storage_key
    disk_path.parent.mkdir(parents=True, exist_ok=True)
    disk_path.write_bytes(data)
    rec = FileRecord(
        id=file_id,
        storage_key=storage_key,
        filename=filename,
        mime=mime,
        size=len(data),
        owner_id="alice",
        chat_id=None,
        created=datetime.now(UTC),
    )
    await store.save_scoped(rec, workspace=workspace)
    return rec, disk_path


async def test_resolve_returns_disk_path_for_scoped_upload(
    tmp_upload_root, store, beanie_upload_db
):
    adapter = LocalStorageAdapter(root=tmp_upload_root)
    resolver = EEUploadResolver(adapter=adapter, meta=store)

    rec, disk_path = await _stash(tmp_upload_root, store, workspace="ws-1")

    resolved = await resolver.resolve(f"/api/v1/uploads/{rec.id}", workspace="ws-1")
    assert resolved == disk_path


async def test_resolve_enforces_workspace_isolation(tmp_upload_root, store, beanie_upload_db):
    adapter = LocalStorageAdapter(root=tmp_upload_root)
    resolver = EEUploadResolver(adapter=adapter, meta=store)

    rec, _ = await _stash(tmp_upload_root, store, workspace="ws-1")

    # Same id, different workspace → not found (not 403, returns None).
    resolved = await resolver.resolve(f"/api/v1/uploads/{rec.id}", workspace="ws-other")
    assert resolved is None


async def test_resolve_returns_none_for_non_upload_url(tmp_upload_root, store, beanie_upload_db):
    adapter = LocalStorageAdapter(root=tmp_upload_root)
    resolver = EEUploadResolver(adapter=adapter, meta=store)

    assert await resolver.resolve("/already/local.pdf", workspace="ws-1") is None
    assert await resolver.resolve("/api/v1/files/abc", workspace="ws-1") is None


async def test_resolve_returns_none_for_soft_deleted_upload(
    tmp_upload_root, store, beanie_upload_db
):
    adapter = LocalStorageAdapter(root=tmp_upload_root)
    resolver = EEUploadResolver(adapter=adapter, meta=store)

    rec, _ = await _stash(tmp_upload_root, store, workspace="ws-1")
    await store.soft_delete_scoped(rec.id, workspace="ws-1")

    assert await resolver.resolve(f"/api/v1/uploads/{rec.id}", workspace="ws-1") is None


async def test_resolve_returns_none_when_blob_missing_on_disk(
    tmp_upload_root, store, beanie_upload_db
):
    adapter = LocalStorageAdapter(root=tmp_upload_root)
    resolver = EEUploadResolver(adapter=adapter, meta=store)

    rec, disk_path = await _stash(tmp_upload_root, store, workspace="ws-1")
    disk_path.unlink()
    assert await resolver.resolve(f"/api/v1/uploads/{rec.id}", workspace="ws-1") is None


async def test_resolve_media_paths_scoped_mixes_pass_drop_resolve(
    tmp_upload_root, store, beanie_upload_db
):
    adapter = LocalStorageAdapter(root=tmp_upload_root)
    resolver = EEUploadResolver(adapter=adapter, meta=store)

    rec, disk_path = await _stash(tmp_upload_root, store, workspace="ws-1")
    ghost = "ghost0000000000000000000000000000"

    result = await resolve_media_paths_scoped(
        [
            f"/api/v1/uploads/{rec.id}",
            "/already/local/path.pdf",
            f"/api/v1/uploads/{ghost}",
        ],
        resolver=resolver,
        workspace="ws-1",
    )
    assert result == [str(disk_path), "/already/local/path.pdf"]
