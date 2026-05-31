"""Tests for pocketpaw.uploads.resolver."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from pocketpaw.uploads.file_store import FileRecord, JSONLFileStore
from pocketpaw.uploads.local import LocalStorageAdapter
from pocketpaw.uploads.resolver import (
    ResolvedMedia,
    UploadResolver,
    parse_upload_url,
    resolve_media_paths,
    resolve_media_with_records,
)


class TestParseUploadUrl:
    def test_extracts_id_from_canonical_url(self) -> None:
        fid = uuid.uuid4().hex
        assert parse_upload_url(f"/api/v1/uploads/{fid}") == fid

    def test_returns_none_for_non_upload_url(self) -> None:
        assert parse_upload_url("/api/v1/files/abc") is None
        assert parse_upload_url("https://example.com/x") is None

    def test_returns_none_for_disk_path(self) -> None:
        assert parse_upload_url("/home/user/image.png") is None
        assert parse_upload_url("C:\\foo\\bar.pdf") is None

    def test_parses_any_id_shaped_segment(self) -> None:
        # parse is permissive; the metadata lookup decides validity.
        assert parse_upload_url("/api/v1/uploads/abc") == "abc"
        assert parse_upload_url("/api/v1/uploads/not-hex-chars") == "not-hex-chars"

    def test_returns_none_for_url_with_trailing_slash_or_segment(self) -> None:
        assert parse_upload_url("/api/v1/uploads/abc/") is None
        assert parse_upload_url("/api/v1/uploads/abc/download") is None

    def test_returns_none_for_empty_string(self) -> None:
        assert parse_upload_url("") is None


class TestUploadResolver:
    @pytest.fixture()
    def resolver(self, tmp_upload_root: Path) -> UploadResolver:
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        meta = JSONLFileStore(path=tmp_upload_root / "_idx.jsonl")
        return UploadResolver(adapter=adapter, meta=meta)

    def _stash(
        self,
        tmp_upload_root: Path,
        resolver: UploadResolver,
        data: bytes = b"hello",
        filename: str = "x.txt",
        mime: str = "text/plain",
    ) -> tuple[FileRecord, Path]:
        """Put a blob on disk and register metadata. Returns (record, disk_path)."""
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
            owner_id="local",
            chat_id=None,
            created=datetime.now(UTC),
        )
        resolver._meta.save(rec)
        return rec, disk_path

    def test_resolve_returns_disk_path_for_stored_upload(
        self, tmp_upload_root: Path, resolver: UploadResolver
    ) -> None:
        rec, disk_path = self._stash(tmp_upload_root, resolver)
        resolved = resolver.resolve(f"/api/v1/uploads/{rec.id}")
        assert resolved == disk_path
        assert resolved is not None
        assert resolved.read_bytes() == b"hello"

    def test_resolve_returns_none_for_missing_metadata(self, resolver: UploadResolver) -> None:
        ghost = uuid.uuid4().hex
        assert resolver.resolve(f"/api/v1/uploads/{ghost}") is None

    def test_resolve_returns_none_when_blob_deleted_from_disk(
        self, tmp_upload_root: Path, resolver: UploadResolver
    ) -> None:
        rec, disk_path = self._stash(tmp_upload_root, resolver)
        disk_path.unlink()
        assert resolver.resolve(f"/api/v1/uploads/{rec.id}") is None

    def test_resolve_returns_none_for_non_upload_url(self, resolver: UploadResolver) -> None:
        assert resolver.resolve("/api/v1/files/abc") is None
        assert resolver.resolve("/already/local/path.txt") is None

    def test_resolve_returns_none_for_soft_deleted_upload(
        self, tmp_upload_root: Path, resolver: UploadResolver
    ) -> None:
        rec, _ = self._stash(tmp_upload_root, resolver)
        resolver._meta.soft_delete(rec.id)
        assert resolver.resolve(f"/api/v1/uploads/{rec.id}") is None


class TestResolveMediaPaths:
    @pytest.fixture()
    def resolver(self, tmp_upload_root: Path) -> UploadResolver:
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        meta = JSONLFileStore(path=tmp_upload_root / "_idx.jsonl")
        return UploadResolver(adapter=adapter, meta=meta)

    def test_mixed_paths_and_urls(self, tmp_upload_root: Path, resolver: UploadResolver) -> None:
        file_id = uuid.uuid4().hex
        storage_key = f"chat/202604/{file_id}.png"
        disk_path = tmp_upload_root / storage_key
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(b"png")
        rec = FileRecord(
            id=file_id,
            storage_key=storage_key,
            filename="x.png",
            mime="image/png",
            size=3,
            owner_id="local",
            chat_id=None,
            created=datetime.now(UTC),
        )
        resolver._meta.save(rec)

        media_in = [
            f"/api/v1/uploads/{file_id}",
            "/already/local/path.pdf",  # passthrough
            "/api/v1/uploads/ghost0000000000000000000000000000",  # unresolvable
        ]
        result = resolve_media_paths(media_in, resolver=resolver)
        assert result == [
            str(disk_path),
            "/already/local/path.pdf",
        ]

    def test_empty_list(self, resolver: UploadResolver) -> None:
        assert resolve_media_paths([], resolver=resolver) == []


class TestResolveMediaWithRecords:
    """Async version that returns path + FileRecord for prompt enrichment."""

    @pytest.mark.asyncio
    async def test_oss_hit_yields_record(self, tmp_upload_root: Path) -> None:
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        meta = JSONLFileStore(path=tmp_upload_root / "_idx.jsonl")

        file_id = uuid.uuid4().hex
        storage_key = f"chat/202604/{file_id}.png"
        disk = tmp_upload_root / storage_key
        disk.parent.mkdir(parents=True, exist_ok=True)
        disk.write_bytes(b"bytes")
        rec = FileRecord(
            id=file_id,
            storage_key=storage_key,
            filename="screenshot.png",
            mime="image/png",
            size=5,
            owner_id="local",
            chat_id=None,
            created=datetime.now(UTC),
        )
        meta.save(rec)

        with (
            patch("pocketpaw.api.v1.uploads._ADAPTER", adapter),
            patch("pocketpaw.api.v1.uploads._META", meta),
        ):
            result = await resolve_media_with_records([f"/api/v1/uploads/{file_id}"])

        assert len(result) == 1
        assert isinstance(result[0], ResolvedMedia)
        assert result[0].path == str(disk)
        assert result[0].record is not None
        assert result[0].record.filename == "screenshot.png"
        assert result[0].record.mime == "image/png"
        assert result[0].record.size == 5

    @pytest.mark.asyncio
    async def test_passthrough_entry_has_none_record(self, tmp_upload_root: Path) -> None:
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        meta = JSONLFileStore(path=tmp_upload_root / "_idx.jsonl")

        with (
            patch("pocketpaw.api.v1.uploads._ADAPTER", adapter),
            patch("pocketpaw.api.v1.uploads._META", meta),
        ):
            result = await resolve_media_with_records(["/already/local/path.pdf"])

        assert result == [ResolvedMedia(path="/already/local/path.pdf", record=None)]

    @pytest.mark.asyncio
    async def test_unresolvable_upload_url_dropped(self, tmp_upload_root: Path) -> None:
        adapter = LocalStorageAdapter(root=tmp_upload_root)
        meta = JSONLFileStore(path=tmp_upload_root / "_idx.jsonl")

        with (
            patch("pocketpaw.api.v1.uploads._ADAPTER", adapter),
            patch("pocketpaw.api.v1.uploads._META", meta),
        ):
            result = await resolve_media_with_records(
                ["/api/v1/uploads/ghost0000000000000000000000000000"]
            )

        assert result == []
