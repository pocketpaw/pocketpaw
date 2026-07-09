# test_listener_auto_tag.py — FL-6 listener integration tests.
# Created: 2026-07-03 — FL-6 "Auto-tagging on ingest". Exercises the
#   index_uploaded_file listener end-to-end against a real (mongomock) store:
#   (1) a file with extractable text gets tags written on the FileUpload row,
#   (2) hide_from_ai=True skips BOTH KB ingest AND tagging, and (3) a re-index
#   unions derived tags with a pre-existing user tag (no clobber). Uses the
#   cloud/uploads conftest fixtures (beanie_upload_db + store).
"""Integration tests for the FL-6 auto-tag path in the FileReady listener."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.realtime.events import FileReady
from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult

from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.asyncio


class _FakeChain:
    def __init__(self, result: ExtractionResult):
        self._result = result
        self.calls: list[tuple[Path, str]] = []

    async def run(self, path: Path, mime: str) -> ExtractionResult:
        self.calls.append((path, mime))
        return self._result


class _FakeAdapter:
    def __init__(self, local_path_value: Path):
        self._local = local_path_value

    def local_path(self, key: str) -> Path | None:
        return self._local

    async def open(self, key: str):  # pragma: no cover — local path wins
        yield b""


def _record(**overrides) -> FileRecord:
    defaults = {
        "id": "f1",
        "storage_key": "chat/202607/aaa.pdf",
        "filename": "invoice.pdf",
        "mime": "application/pdf",
        "size": 1,
        "owner_id": "u1",
        "chat_id": "c1",
        "created": datetime.now(UTC),
    }
    defaults.update(overrides)
    return FileRecord(**defaults)


def _wire(monkeypatch, *, chain: _FakeChain, adapter: _FakeAdapter, ingest: AsyncMock):
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads import listeners

    monkeypatch.setattr("pocketpaw_ee.cloud.extraction.build_chain", lambda settings: chain)
    monkeypatch.setattr(listeners, "_resolve_adapter", lambda: adapter)
    monkeypatch.setattr(kn.KnowledgeService, "ingest_text_to_scope", ingest)


def _event() -> FileReady:
    return FileReady(
        data={
            "workspace_id": "w1",
            "file_id": "f1",
            "filename": "invoice.pdf",
            "mime": "application/pdf",
            "storage_key": "chat/202607/aaa.pdf",
        }
    )


async def test_upload_with_text_gets_tags_written(monkeypatch, store, tmp_path):
    """Extractable text → tags land on the FileUpload row."""
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), workspace="w1")

    fake_path = tmp_path / "invoice.pdf"
    fake_path.write_bytes(b"unused; chain mocked")
    chain = _FakeChain(
        ExtractionResult(
            title="Quarterly Invoice",
            text="invoice invoice payment payment amount total due balance",
            backend="local",
        )
    )
    ingest = AsyncMock(return_value={"id": "art-1"})
    _wire(monkeypatch, chain=chain, adapter=_FakeAdapter(fake_path), ingest=ingest)

    await index_uploaded_file(_event())

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc is not None
    assert doc.tags, "expected auto-tags to be written"
    assert "invoice" in doc.tags
    # Normalized: lowercase, deduped.
    assert doc.tags == [t.lower() for t in doc.tags]
    assert len(doc.tags) == len(set(doc.tags))
    ingest.assert_awaited_once()


async def test_hide_from_ai_skips_ingest_and_tags(monkeypatch, store, tmp_path):
    """hide_from_ai=True → NO KB ingest, NO tags, extraction never runs."""
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), workspace="w1")
    await store.set_library_metadata("f1", "w1", hide_from_ai=True)

    fake_path = tmp_path / "invoice.pdf"
    fake_path.write_bytes(b"unused")
    chain = _FakeChain(ExtractionResult(text="secret content", backend="local"))
    ingest = AsyncMock()
    _wire(monkeypatch, chain=chain, adapter=_FakeAdapter(fake_path), ingest=ingest)

    await index_uploaded_file(_event())

    # Neither extraction nor ingest ran.
    assert chain.calls == []
    ingest.assert_not_awaited()
    # No tags written.
    doc = await store.get_doc_scoped("f1", "w1")
    assert doc is not None
    assert doc.tags == []


async def test_reindex_unions_with_existing_user_tag(monkeypatch, store, tmp_path):
    """A pre-existing user tag survives a re-index (union, no clobber)."""
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), workspace="w1")
    await store.set_library_metadata("f1", "w1", tags=["important"])

    fake_path = tmp_path / "invoice.pdf"
    fake_path.write_bytes(b"unused")
    chain = _FakeChain(
        ExtractionResult(
            title="Budget Forecast",
            text="budget budget forecast forecast revenue projection",
            backend="local",
        )
    )
    ingest = AsyncMock(return_value={"id": "art-1"})
    _wire(monkeypatch, chain=chain, adapter=_FakeAdapter(fake_path), ingest=ingest)

    await index_uploaded_file(_event())

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc is not None
    assert "important" in doc.tags, "user tag must survive re-index"
    assert "budget" in doc.tags, "derived tag should be unioned in"


async def test_empty_extraction_no_tags_but_no_crash(monkeypatch, store, tmp_path):
    """Empty extracted text → no tags derived, no crash, no ingest."""
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), workspace="w1")

    fake_path = tmp_path / "blank.pdf"
    fake_path.write_bytes(b"unused")
    chain = _FakeChain(ExtractionResult(text="   ", backend="local"))
    ingest = AsyncMock()
    _wire(monkeypatch, chain=chain, adapter=_FakeAdapter(fake_path), ingest=ingest)

    await index_uploaded_file(_event())

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc is not None
    assert doc.tags == []
    ingest.assert_not_awaited()
