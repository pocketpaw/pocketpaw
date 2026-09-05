# test_listener_links.py — files vault listener tests (feat/files-links, B2).
# Created: 2026-09-05. Exercises index_uploaded_file against a real (mongomock)
#   store: a markdown note gets link_names and its #tags merged with the
#   existing user tags; a note tag beats derived keyword tags at the MAX_TAGS
#   cap; a hidden file gets none; a non-text mime gets none; a parser crash
#   still tags + indexes; and a re-index that lands under a new kb article id
#   removes the old article (same id -> no remove), so one file tracks one
#   article no matter how many FileReady events it sees.
"""Integration tests for the note-links path in the FileReady listener."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.realtime.events import FileReady
from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult

from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.asyncio

NOTE = "---\ntags: [Project]\n---\n# Plan\nSee [[Alpha]] and [[Beta|b]] #todo\n"


class _FakeChain:
    def __init__(self, result: ExtractionResult):
        self._result = result

    async def run(self, path: Path, mime: str) -> ExtractionResult:
        return self._result


class _FakeAdapter:
    def __init__(self, local_path_value: Path):
        self._local = local_path_value

    def local_path(self, key: str) -> Path | None:
        return self._local

    async def open(self, key: str):  # pragma: no cover
        yield b""


def _record(mime: str = "text/markdown", filename: str = "plan.md") -> FileRecord:
    return FileRecord(
        id="f1",
        storage_key="editor/w1/aaa",
        filename=filename,
        mime=mime,
        size=1,
        owner_id="u1",
        chat_id=None,
        created=datetime.now(UTC),
    )


def _wire(monkeypatch, tmp_path, *, text: str, ingest: AsyncMock, mime="text/markdown"):
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads import listeners

    fake_path = tmp_path / "plan.md"
    fake_path.write_text(text)
    chain = _FakeChain(ExtractionResult(text=text, backend="local"))
    monkeypatch.setattr("pocketpaw_ee.cloud.extraction.build_chain", lambda settings: chain)
    monkeypatch.setattr(listeners, "_resolve_adapter", lambda: _FakeAdapter(fake_path))
    monkeypatch.setattr(kn.KnowledgeService, "ingest_text_to_scope", ingest)
    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)
    return remove


def _event(mime: str = "text/markdown") -> FileReady:
    return FileReady(
        data={
            "workspace_id": "w1",
            "file_id": "f1",
            "filename": "plan.md",
            "mime": mime,
            "storage_key": "editor/w1/aaa",
        }
    )


async def test_markdown_note_gets_links_and_tags_merged(monkeypatch, store, tmp_path):
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), workspace="w1")
    await store.set_library_metadata("f1", "w1", tags=["mine"])
    _wire(monkeypatch, tmp_path, text=NOTE, ingest=AsyncMock(return_value={"id": "art-1"}))

    await index_uploaded_file(_event())

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.link_names == ["alpha", "beta"]
    assert doc.tags[:3] == ["mine", "todo", "project"]


async def test_note_tag_beats_derived_keywords_at_the_cap(monkeypatch, store, tmp_path):
    """Eight repeated keywords fill MAX_TAGS; the author's #tag still lands."""
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), workspace="w1")
    words = " ".join(
        f"{w} {w} {w}" for w in "alpha bravo charlie delta echo foxtrot golf hotel".split()
    )
    _wire(monkeypatch, tmp_path, text=f"{words}\n#keeper", ingest=AsyncMock(return_value={}))

    await index_uploaded_file(_event())

    doc = await store.get_doc_scoped("f1", "w1")
    assert "keeper" in doc.tags
    assert len(doc.tags) <= 8


async def test_hidden_note_gets_no_links(monkeypatch, store, tmp_path):
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), workspace="w1")
    await store.set_library_metadata("f1", "w1", hide_from_ai=True)
    _wire(monkeypatch, tmp_path, text=NOTE, ingest=AsyncMock())

    await index_uploaded_file(_event())

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.link_names == []
    assert doc.tags == []


async def test_non_text_mime_gets_no_links(monkeypatch, store, tmp_path):
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(mime="application/pdf", filename="plan.pdf"), workspace="w1")
    _wire(monkeypatch, tmp_path, text=NOTE, ingest=AsyncMock(return_value={"id": "a"}))

    await index_uploaded_file(_event("application/pdf"))

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.link_names == []


async def test_parser_crash_still_tags_and_indexes(monkeypatch, store, tmp_path):
    from pocketpaw_ee.cloud.uploads import links
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), workspace="w1")
    ingest = AsyncMock(return_value={"id": "art-1"})
    _wire(monkeypatch, tmp_path, text="plan plan plan [[Alpha]]", ingest=ingest)

    def boom(_text):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(links, "parse_note_links", boom)

    await index_uploaded_file(_event())

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.link_names == []
    assert "plan" in doc.tags
    assert doc.kb_article_id == "art-1"
    ingest.assert_awaited_once()


async def test_reindex_under_new_article_id_removes_the_old_one(monkeypatch, store, tmp_path):
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), workspace="w1")
    ingest = AsyncMock(side_effect=[{"id": "art-1"}, {"id": "art-2"}, {"id": "art-2"}])
    remove = _wire(monkeypatch, tmp_path, text=NOTE, ingest=ingest)

    await index_uploaded_file(_event())
    remove.assert_not_awaited()
    await index_uploaded_file(_event())
    remove.assert_awaited_once_with("workspace:w1", "art-1")
    await index_uploaded_file(_event())
    assert remove.await_count == 1  # same id twice: nothing to purge

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.kb_article_id == "art-2"
    assert doc.kb_scope == "workspace:w1"
    assert doc.link_names == ["alpha", "beta"]


async def test_short_typed_hashtag_survives_the_keyword_floor(monkeypatch, store, tmp_path):
    """#q3 is two characters. The 3-char floor is for keyword noise, not for a
    tag a person typed; seen live 2026-09-05 when #q3 vanished from a note."""
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), workspace="w1")
    _wire(
        monkeypatch,
        tmp_path,
        text="Pricing notes, see [[Roadmap]] and #q3 #ai",
        ingest=AsyncMock(return_value={"id": "art-1"}),
    )

    await index_uploaded_file(_event())

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc.link_names == ["roadmap"]
    assert "q3" in doc.tags and "ai" in doc.tags
