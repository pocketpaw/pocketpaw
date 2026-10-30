from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi import UploadFile

from pocketpaw.uploads.adapter import StorageAdapter, StoredObject
from pocketpaw.uploads.config import UploadSettings
from pocketpaw.uploads.errors import NotFound

pytestmark = pytest.mark.asyncio

PNG = b"\x89PNG\r\n\x1a\n" + b"rest"


class _MemAdapter(StorageAdapter):
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key, stream, mime):
        buf = b""
        async for c in stream:
            buf += c
        self.blobs[key] = buf
        return StoredObject(key=key, size=len(buf), mime=mime)

    async def open(self, key):
        if key not in self.blobs:
            raise NotFound()
        yield self.blobs[key]

    async def delete(self, key):
        self.blobs.pop(key, None)

    async def exists(self, key):
        return key in self.blobs


def _upload(content: bytes, filename: str, mime: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers={"content-type": mime},  # type: ignore[arg-type]
    )


class TestEEUploadService:
    async def test_upload_stores_record_in_workspace(self, store, tmp_path: Path):
        from pocketpaw_ee.cloud.uploads.service import EEUploadService

        svc = EEUploadService(
            adapter=_MemAdapter(),
            meta=store,
            cfg=UploadSettings(local_root=tmp_path),
        )
        rec = await svc.upload(
            _upload(PNG, "cat.png", "image/png"), owner_id="u1", chat_id="c1", workspace="w1"
        )
        assert rec.owner_id == "u1"
        got = await store.get_scoped(rec.id, workspace="w1")
        assert got is not None

    async def test_stream_enforces_workspace(self, store, tmp_path: Path):
        from pocketpaw_ee.cloud.uploads.service import EEUploadService

        svc = EEUploadService(
            adapter=_MemAdapter(),
            meta=store,
            cfg=UploadSettings(local_root=tmp_path),
        )
        rec = await svc.upload(
            _upload(PNG, "cat.png", "image/png"), owner_id="u1", chat_id="c1", workspace="w1"
        )
        with pytest.raises(NotFound):
            await svc.stream(rec.id, requester_id="u1", workspace="w2")

    async def test_stream_happy_path(self, store, tmp_path: Path):
        from pocketpaw_ee.cloud.uploads.service import EEUploadService

        adapter = _MemAdapter()
        svc = EEUploadService(
            adapter=adapter,
            meta=store,
            cfg=UploadSettings(local_root=tmp_path),
        )
        rec = await svc.upload(
            _upload(PNG, "cat.png", "image/png"), owner_id="u1", chat_id="c1", workspace="w1"
        )
        got_rec, it = await svc.stream(rec.id, requester_id="u1", workspace="w1")
        chunks = [c async for c in it]
        assert b"".join(chunks) == PNG
        assert got_rec.id == rec.id

    async def test_delete_owner_in_workspace(self, store, tmp_path: Path):
        from pocketpaw_ee.cloud.uploads.service import EEUploadService

        svc = EEUploadService(
            adapter=_MemAdapter(),
            meta=store,
            cfg=UploadSettings(local_root=tmp_path),
        )
        rec = await svc.upload(
            _upload(PNG, "cat.png", "image/png"), owner_id="u1", chat_id="c1", workspace="w1"
        )
        await svc.delete(rec.id, requester_id="u1", workspace="w1")
        with pytest.raises(NotFound):
            await svc.stream(rec.id, requester_id="u1", workspace="w1")

    async def test_bulk_upload(self, store, tmp_path: Path):
        from pocketpaw_ee.cloud.uploads.service import EEUploadService

        svc = EEUploadService(
            adapter=_MemAdapter(),
            meta=store,
            cfg=UploadSettings(local_root=tmp_path),
        )
        files = [
            _upload(PNG, "a.png", "image/png"),
            _upload(PNG, "b.png", "image/png"),
        ]
        result = await svc.upload_many(files, owner_id="u1", chat_id=None, workspace="w1")
        assert len(result.uploaded) == 2
        assert len(result.failed) == 0
        # Both persisted in Mongo under workspace
        for rec in result.uploaded:
            got = await store.get_scoped(rec.id, workspace="w1")
            assert got is not None
