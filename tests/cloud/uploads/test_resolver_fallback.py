"""Tests for the OSS→EE Mongo fallback in ``resolve_media_paths_any``.

Scenario: uploads go through the EE router (workspace-scoped Mongo) but
chat still goes through the OSS ``/chat/stream`` endpoint. Without fallback
the OSS JSONL lookup misses every EE upload.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from pocketpaw.uploads.file_store import FileRecord, JSONLFileStore
from pocketpaw.uploads.local import LocalStorageAdapter
from pocketpaw.uploads.resolver import resolve_media_paths_any

pytestmark = pytest.mark.asyncio


async def test_fallback_resolves_from_ee_mongo_when_oss_misses(
    tmp_upload_root: Path, store, beanie_upload_db
):
    """File in Mongo but not JSONL still resolves via the fallback path."""
    adapter = LocalStorageAdapter(root=tmp_upload_root)
    oss_meta = JSONLFileStore(path=tmp_upload_root / "_idx.jsonl")

    # Stash only in Mongo (simulates upload via EE router).
    file_id = uuid.uuid4().hex
    storage_key = f"chat/202604/{file_id}.png"
    disk = tmp_upload_root / storage_key
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(b"pngbytes")
    await store.save_scoped(
        FileRecord(
            id=file_id,
            storage_key=storage_key,
            filename="x.png",
            mime="image/png",
            size=8,
            owner_id="u1",
            chat_id="__paw-runtime-dm__",
            created=datetime.now(UTC),
        ),
        workspace="ws-1",
    )

    # Stub OSS + EE module-level singletons for the resolver.
    with (
        patch("pocketpaw.api.v1.uploads._ADAPTER", adapter),
        patch("pocketpaw.api.v1.uploads._META", oss_meta),
        patch("pocketpaw_ee.cloud.uploads.router._ADAPTER", adapter),
        patch("pocketpaw_ee.cloud.uploads.router._META", store),
    ):
        result = await resolve_media_paths_any([f"/api/v1/uploads/{file_id}"])

    assert result == [str(disk)]


async def test_fallback_returns_none_when_neither_store_has_id(
    tmp_upload_root: Path, store, beanie_upload_db
):
    adapter = LocalStorageAdapter(root=tmp_upload_root)
    oss_meta = JSONLFileStore(path=tmp_upload_root / "_idx.jsonl")
    ghost_url = "/api/v1/uploads/ghost0000000000000000000000000000"

    with (
        patch("pocketpaw.api.v1.uploads._ADAPTER", adapter),
        patch("pocketpaw.api.v1.uploads._META", oss_meta),
        patch("pocketpaw_ee.cloud.uploads.router._ADAPTER", adapter),
        patch("pocketpaw_ee.cloud.uploads.router._META", store),
    ):
        result = await resolve_media_paths_any([ghost_url])

    assert result == []


async def test_fallback_ignores_soft_deleted_ee_record(
    tmp_upload_root: Path, store, beanie_upload_db
):
    adapter = LocalStorageAdapter(root=tmp_upload_root)
    oss_meta = JSONLFileStore(path=tmp_upload_root / "_idx.jsonl")

    file_id = uuid.uuid4().hex
    storage_key = f"chat/202604/{file_id}.png"
    disk = tmp_upload_root / storage_key
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.write_bytes(b"x")
    await store.save_scoped(
        FileRecord(
            id=file_id,
            storage_key=storage_key,
            filename="x.png",
            mime="image/png",
            size=1,
            owner_id="u1",
            chat_id=None,
            created=datetime.now(UTC),
        ),
        workspace="ws-1",
    )
    await store.soft_delete_scoped(file_id, workspace="ws-1")

    with (
        patch("pocketpaw.api.v1.uploads._ADAPTER", adapter),
        patch("pocketpaw.api.v1.uploads._META", oss_meta),
        patch("pocketpaw_ee.cloud.uploads.router._ADAPTER", adapter),
        patch("pocketpaw_ee.cloud.uploads.router._META", store),
    ):
        result = await resolve_media_paths_any([f"/api/v1/uploads/{file_id}"])

    assert result == []


async def test_get_unscoped_returns_record_across_workspaces(
    tmp_upload_root: Path, store, beanie_upload_db
):
    """``get_unscoped`` finds a file_id regardless of workspace."""
    file_id = uuid.uuid4().hex
    await store.save_scoped(
        FileRecord(
            id=file_id,
            storage_key=f"chat/202604/{file_id}.txt",
            filename="x.txt",
            mime="text/plain",
            size=1,
            owner_id="u1",
            chat_id=None,
            created=datetime.now(UTC),
        ),
        workspace="ws-private",
    )

    found = await store.get_unscoped(file_id)
    assert found is not None
    assert found.id == file_id
