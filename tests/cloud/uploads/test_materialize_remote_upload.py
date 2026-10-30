# tests/cloud/uploads/test_materialize_remote_upload.py — a URL-attached file
# reaching a local path on an adapter that has none.
#
# Created 2026-09-15 (feat/chat-image-wiring). New file.
#
# ``materialize_to_local_path`` is the hinge every URL-attached file turns on.
# A chat attachment arrives as ``{"url": "/api/v1/uploads/<id>"}`` and both
# readers — ``_build_attachments_block`` for documents and
# ``resolve_turn_images`` for pictures — reach the bytes through
# ``EEUploadResolver.open_local_for_url``, which calls this. It had no direct
# test, and it is the half that behaves differently in production than on a
# laptop: local disk yields its real path, while S3 has no path at all and the
# blob has to be streamed into a temp file first.
#
# Every failure here is SILENT by design. The contract is "yield None and let
# the caller skip this entry", so a broken remote branch does not raise: the
# file is simply absent, the model is told nothing about it, and the turn looks
# normal. That is what makes it worth pinning rather than assuming.
#
# The suffix is load-bearing, not cosmetic. The extraction chain routes on file
# extension, so a .pdf that lands in a temp file named ``paw-upload-xyz`` is
# handed to the wrong extractor (or none) and yields nothing — which reads as
# "the agent cannot see my document".

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pocketpaw_ee.cloud.uploads.resolver import (
    EEUploadResolver,
    materialize_to_local_path,
)

from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.asyncio

PDF = b"%PDF-1.7\n" + b"x" * 512


class _RemoteAdapter:
    """An S3-shaped adapter: no on-disk path, bytes only through ``open``."""

    def __init__(self, blobs: dict[str, bytes]) -> None:
        self.blobs = blobs
        self.opened: list[str] = []

    def local_path(self, key: str) -> Path | None:
        return None

    def open(self, key: str) -> AsyncIterator[bytes]:
        self.opened.append(key)

        async def _gen() -> AsyncIterator[bytes]:
            data = self.blobs[key]
            # Chunked, as a real S3 body arrives — a reader that assumes one
            # chunk truncates every file past the first block.
            for i in range(0, len(data), 64):
                yield data[i : i + 64]

        return _gen()


class _LocalAdapter:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.opened: list[str] = []

    def local_path(self, key: str) -> Path | None:
        return self.path

    def open(self, key: str) -> AsyncIterator[bytes]:  # pragma: no cover
        self.opened.append(key)
        raise AssertionError("a local adapter must not be streamed")


class _BrokenAdapter:
    def local_path(self, key: str) -> Path | None:
        return None

    def open(self, key: str) -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            yield b"partial"
            raise OSError("connection reset mid-body")

        return _gen()


class _Meta:
    def __init__(self, rec: FileRecord | None, *, workspace: str = "ws-1") -> None:
        self._rec = rec
        self._workspace = workspace
        self.asked: list[tuple[str, str]] = []

    async def get_scoped(self, file_id: str, workspace: str) -> FileRecord | None:
        self.asked.append((file_id, workspace))
        if self._rec is None or workspace != self._workspace:
            return None
        return self._rec


def _rec(filename: str = "report.pdf", mime: str = "application/pdf") -> FileRecord:
    return FileRecord(
        id="f1",
        storage_key="ws-1/f1",
        filename=filename,
        mime=mime,
        size=len(PDF),
        owner_id="u1",
        chat_id=None,
        created=datetime.now(UTC),
    )


class TestTheRemoteBranch:
    async def test_an_s3_blob_becomes_a_readable_local_file(self) -> None:
        adapter = _RemoteAdapter({"ws-1/f1": PDF})
        async with materialize_to_local_path(
            adapter, "ws-1/f1", mime="application/pdf", filename="report.pdf"
        ) as path:
            assert path is not None
            assert path.read_bytes() == PDF, "every chunk must land, not just the first"

    async def test_the_temp_file_keeps_the_pdf_suffix(self) -> None:
        # The extraction chain routes on extension. Lose it and the document is
        # handed to the wrong extractor, which is indistinguishable from the
        # agent being unable to read the file.
        adapter = _RemoteAdapter({"ws-1/f1": PDF})
        async with materialize_to_local_path(
            adapter, "ws-1/f1", mime="application/pdf", filename="report.pdf"
        ) as path:
            assert path is not None
            assert path.suffix == ".pdf"

    async def test_a_filename_with_no_extension_falls_back_to_the_mime(self) -> None:
        adapter = _RemoteAdapter({"ws-1/f1": PDF})
        async with materialize_to_local_path(
            adapter, "ws-1/f1", mime="application/pdf", filename="scan"
        ) as path:
            assert path is not None
            assert path.suffix == ".pdf"

    async def test_the_temp_file_is_gone_afterwards(self) -> None:
        adapter = _RemoteAdapter({"ws-1/f1": PDF})
        async with materialize_to_local_path(
            adapter, "ws-1/f1", mime="application/pdf", filename="report.pdf"
        ) as path:
            assert path is not None
            held = path
        assert not held.exists(), "a turn's temp copies must not accumulate on disk"

    async def test_a_stream_that_dies_midway_yields_none(self) -> None:
        # Per-file isolation: one unreadable attachment must not raise into the
        # turn. The caller skips the entry and the rest still ride.
        async with materialize_to_local_path(
            _BrokenAdapter(), "ws-1/f1", mime="application/pdf", filename="report.pdf"
        ) as path:
            assert path is None


class TestTheLocalBranch:
    async def test_a_local_adapter_is_used_in_place_and_never_streamed(
        self, tmp_path: Path
    ) -> None:
        real = tmp_path / "report.pdf"
        real.write_bytes(PDF)
        adapter = _LocalAdapter(real)
        async with materialize_to_local_path(
            adapter, "ws-1/f1", mime="application/pdf", filename="report.pdf"
        ) as path:
            assert path == real
        assert adapter.opened == []
        assert real.exists(), "the stored blob itself must survive the context"

    async def test_an_empty_storage_key_yields_none(self) -> None:
        async with materialize_to_local_path(
            _RemoteAdapter({}), "", mime="application/pdf", filename="report.pdf"
        ) as path:
            assert path is None


class TestTheWholeUrlChain:
    async def test_a_url_attached_pdf_on_s3_resolves_to_record_and_bytes(self) -> None:
        # The end-to-end shape both attachment readers use.
        adapter = _RemoteAdapter({"ws-1/f1": PDF})
        resolver = EEUploadResolver(adapter=adapter, meta=_Meta(_rec()))
        async with resolver.open_local_for_url("/api/v1/uploads/f1", workspace="ws-1") as resolved:
            assert resolved is not None
            rec, path = resolved
            assert rec.filename == "report.pdf"
            assert path.read_bytes() == PDF

    async def test_another_workspace_gets_nothing(self) -> None:
        adapter = _RemoteAdapter({"ws-1/f1": PDF})
        resolver = EEUploadResolver(adapter=adapter, meta=_Meta(_rec()))
        async with resolver.open_local_for_url(
            "/api/v1/uploads/f1", workspace="ws-other"
        ) as resolved:
            assert resolved is None
        assert adapter.opened == [], "a cross-tenant miss must not touch storage"

    @pytest.mark.parametrize(
        "url",
        [
            "https://cdn.example.com/api/v1/uploads/f1",
            "/api/v1/uploads/f1?w=64&h=64",
            "/api/v1/uploads/",
            "blob:abc123",
            "",
        ],
    )
    async def test_a_url_the_parser_does_not_accept_yields_none(self, url: str) -> None:
        # Documents the shapes that resolve to NOTHING, silently. The canonical
        # form is what ``_record_to_dict`` mints, so this is the blast radius if
        # a producer ever sends an absolute URL or appends a query string (the
        # thumbnail URLs from ``/grant`` carry one).
        resolver = EEUploadResolver(adapter=_RemoteAdapter({}), meta=_Meta(_rec()))
        async with resolver.open_local_for_url(url, workspace="ws-1") as resolved:
            assert resolved is None
