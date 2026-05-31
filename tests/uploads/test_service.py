from __future__ import annotations

import io
from pathlib import Path

import pytest
from fastapi import UploadFile

from pocketpaw.uploads.adapter import StorageAdapter, StoredObject
from pocketpaw.uploads.config import UploadSettings
from pocketpaw.uploads.errors import (
    EmptyFile,
    NotFound,
    TooLarge,
    UnsupportedMime,
)
from pocketpaw.uploads.file_store import JSONLFileStore
from pocketpaw.uploads.service import UploadService

# --- Fake adapter --------------------------------------------------------


class _FakeAdapter(StorageAdapter):
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    async def put(self, key, stream, mime):
        data = b""
        async for chunk in stream:
            data += chunk
        self.blobs[key] = data
        return StoredObject(key=key, size=len(data), mime=mime)

    async def open(self, key):
        if key not in self.blobs:
            raise NotFound()
        yield self.blobs[key]

    async def delete(self, key):
        self.blobs.pop(key, None)

    async def exists(self, key):
        return key in self.blobs


# --- Helpers -------------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"rest"
JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"rest"


def _upload(content: bytes, filename: str, content_type: str) -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers={"content-type": content_type},  # type: ignore[arg-type]
    )


@pytest.fixture()
def service(tmp_path: Path):
    adapter = _FakeAdapter()
    meta = JSONLFileStore(path=tmp_path / "idx.jsonl")
    cfg = UploadSettings(local_root=tmp_path)
    return UploadService(adapter=adapter, meta=meta, cfg=cfg), adapter, meta


# --- Tests ---------------------------------------------------------------


class TestUploadServiceSingle:
    async def test_happy_path_returns_record(self, service):
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "cat.png", "image/png")
        rec = await svc.upload(file, owner_id="u1", chat_id="c1")
        assert rec.filename == "cat.png"
        assert rec.mime == "image/png"
        assert rec.size == len(PNG_MAGIC)
        assert rec.owner_id == "u1"
        assert rec.chat_id == "c1"
        assert rec.id

    async def test_rejects_oversize(self, service):
        svc, _, _ = service
        svc._cfg = UploadSettings(max_file_bytes=10, local_root=svc._cfg.local_root)
        file = _upload(b"x" * 100, "big.bin", "application/octet-stream")
        with pytest.raises(TooLarge):
            await svc.upload(file, owner_id="u1", chat_id=None)

    async def test_rejects_disallowed_mime(self, service):
        svc, _, _ = service
        file = _upload(b"<svg/>", "x.svg", "image/svg+xml")
        with pytest.raises(UnsupportedMime):
            await svc.upload(file, owner_id="u1", chat_id=None)

    async def test_rejects_empty_file(self, service):
        svc, _, _ = service
        file = _upload(b"", "empty.txt", "text/plain")
        with pytest.raises(EmptyFile):
            await svc.upload(file, owner_id="u1", chat_id=None)

    async def test_magic_byte_sniff_overrides_content_type(self, service):
        # Client claims image/jpeg but bytes are PNG; saved mime should be image/png.
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "x.jpg", "image/jpeg")
        rec = await svc.upload(file, owner_id="u1", chat_id=None)
        assert rec.mime == "image/png"

    async def test_filename_with_path_separators_sanitized(self, service):
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "../evil.png", "image/png")
        rec = await svc.upload(file, owner_id="u1", chat_id=None)
        assert rec.filename == "evil.png"


class TestUploadServiceBulk:
    async def test_all_succeed(self, service):
        svc, _, _ = service
        files = [
            _upload(PNG_MAGIC, "a.png", "image/png"),
            _upload(JPEG_MAGIC, "b.jpg", "image/jpeg"),
            _upload(b"hi", "c.txt", "text/plain"),
        ]
        result = await svc.upload_many(files, owner_id="u1", chat_id=None)
        assert len(result.uploaded) == 3
        assert len(result.failed) == 0

    async def test_partial_failure(self, service):
        svc, _, _ = service
        svc._cfg = UploadSettings(max_file_bytes=100, local_root=svc._cfg.local_root)
        files = [
            _upload(PNG_MAGIC, "good.png", "image/png"),
            _upload(b"x" * 500, "big.bin", "application/octet-stream"),
            _upload(b"<svg/>", "bad.svg", "image/svg+xml"),
        ]
        result = await svc.upload_many(files, owner_id="u1", chat_id=None)
        assert len(result.uploaded) == 1
        assert result.uploaded[0].filename == "good.png"
        assert len(result.failed) == 2
        codes = {f.code for f in result.failed}
        assert codes == {"too_large", "unsupported_mime"}

    async def test_empty_batch_raises(self, service):
        svc, _, _ = service
        with pytest.raises(ValueError, match="empty"):
            await svc.upload_many([], owner_id="u1", chat_id=None)

    async def test_batch_over_cap_raises(self, service):
        svc, _, _ = service
        svc._cfg = UploadSettings(max_files_per_batch=2, local_root=svc._cfg.local_root)
        files = [_upload(PNG_MAGIC, f"{i}.png", "image/png") for i in range(3)]
        with pytest.raises(ValueError, match="too many"):
            await svc.upload_many(files, owner_id="u1", chat_id=None)


class TestStreamAndDelete:
    async def test_stream_happy_path(self, service):
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "cat.png", "image/png")
        rec = await svc.upload(file, owner_id="u1", chat_id=None)
        got_rec, it = await svc.stream(rec.id, requester_id="u1")
        chunks = [c async for c in it]
        assert b"".join(chunks) == PNG_MAGIC
        assert got_rec.id == rec.id

    async def test_stream_wrong_owner_returns_not_found(self, service):
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "cat.png", "image/png")
        rec = await svc.upload(file, owner_id="u1", chat_id=None)
        with pytest.raises(NotFound):
            await svc.stream(rec.id, requester_id="someone-else")

    async def test_stream_missing_raises_not_found(self, service):
        svc, _, _ = service
        with pytest.raises(NotFound):
            await svc.stream("nope", requester_id="u1")

    async def test_delete_owner_succeeds_idempotent(self, service):
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "cat.png", "image/png")
        rec = await svc.upload(file, owner_id="u1", chat_id=None)
        await svc.delete(rec.id, requester_id="u1")
        with pytest.raises(NotFound):
            await svc.stream(rec.id, requester_id="u1")
        with pytest.raises(NotFound):
            await svc.delete(rec.id, requester_id="u1")

    async def test_delete_non_owner_raises_not_found(self, service):
        svc, _, _ = service
        file = _upload(PNG_MAGIC, "cat.png", "image/png")
        rec = await svc.upload(file, owner_id="u1", chat_id=None)
        with pytest.raises(NotFound):
            await svc.delete(rec.id, requester_id="someone-else")
