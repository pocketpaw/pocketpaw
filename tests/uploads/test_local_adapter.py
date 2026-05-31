from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pocketpaw.uploads.errors import AccessDenied, NotFound, StorageFailure
from pocketpaw.uploads.local import LocalStorageAdapter


async def _astream(chunks: list[bytes]) -> AsyncIterator[bytes]:
    for c in chunks:
        yield c


class TestLocalStorageAdapter:
    async def test_put_writes_bytes_and_returns_size(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        obj = await adapter.put("chat/202604/abc.png", _astream([b"hello"]), "image/png")
        assert obj.key == "chat/202604/abc.png"
        assert obj.size == 5
        assert obj.mime == "image/png"
        assert (tmp_upload_root / "chat" / "202604" / "abc.png").read_bytes() == b"hello"

    async def test_put_creates_parent_dirs(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        await adapter.put("a/b/c/d.bin", _astream([b"x"]), "application/octet-stream")
        assert (tmp_upload_root / "a" / "b" / "c" / "d.bin").exists()

    async def test_put_concatenates_chunks(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        obj = await adapter.put(
            "k/file.bin", _astream([b"foo", b"bar", b"baz"]), "application/octet-stream"
        )
        assert obj.size == 9
        assert (tmp_upload_root / "k/file.bin").read_bytes() == b"foobarbaz"

    async def test_put_atomic_no_partial_on_stream_error(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)

        async def bad_stream() -> AsyncIterator[bytes]:
            yield b"part1"
            raise RuntimeError("boom")

        with pytest.raises(StorageFailure):
            await adapter.put("k/file.bin", bad_stream(), "application/octet-stream")

        # Final file must NOT exist
        assert not (tmp_upload_root / "k" / "file.bin").exists()

    async def test_open_streams_bytes(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        await adapter.put("k/file.bin", _astream([b"hello world"]), "application/octet-stream")
        chunks: list[bytes] = [c async for c in adapter.open("k/file.bin")]
        assert b"".join(chunks) == b"hello world"

    async def test_open_missing_raises_not_found(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        with pytest.raises(NotFound):
            _ = [c async for c in adapter.open("nope")]

    async def test_delete_idempotent(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        await adapter.put("k/file.bin", _astream([b"x"]), "application/octet-stream")
        await adapter.delete("k/file.bin")
        await adapter.delete("k/file.bin")  # second call: no error
        assert not (tmp_upload_root / "k" / "file.bin").exists()

    async def test_exists_true_after_put(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        assert await adapter.exists("k/file.bin") is False
        await adapter.put("k/file.bin", _astream([b"x"]), "application/octet-stream")
        assert await adapter.exists("k/file.bin") is True

    async def test_put_rejects_path_traversal_key(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        with pytest.raises(AccessDenied):
            await adapter.put("../../evil.bin", _astream([b"x"]), "application/octet-stream")

    async def test_open_rejects_path_traversal_key(self, tmp_upload_root: Path):
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        with pytest.raises(AccessDenied):
            _ = [c async for c in adapter.open("../outside.bin")]
