# test_kb_prune_on_delete.py — Coupling T-1 "file delete prunes extracted knowledge".
# Created: 2026-08-05. Covers the full slice:
#   (1) The FileReady ingest stamps the upload's file_id into the kb-go
#       --source string ("<filename>#file:<file_id>") so the article joins
#       back to the file; the lang hint survives the stamp.
#   (2) The FileDeleted subscriber reads the TOMBSTONED row (the service
#       soft-deletes before emitting), calls KnowledgeService.remove_article
#       with the tracked scope + article id, and clears the tracking.
#   (3) Delete with no tracked article (or no resolvable row / bad payload)
#       is a safe no-op — remove_article never called.
#   (4) A purge failure keeps the tracking so a sweeper can re-purge.
#   (5) register_upload_listeners subscribes the prune handler to
#       FileDeleted.EVENT_TYPE (the event service.delete emits).
# Spy-don't-mock: remove_article is spied at the KnowledgeService seam (the
# subprocess boundary), the store and listener under test run real code
# against the mongomock-backed Beanie fixtures from conftest.
"""Tests for the Coupling T-1 KB prune-on-delete path."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.realtime.events import FileDeleted, FileReady
from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult

from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.asyncio


# --- fixtures / helpers (mirrors test_kb_purge.py) ------------------------


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

    async def open(self, key: str):  # pragma: no cover — local path wins
        yield b""


def _record(**overrides) -> FileRecord:
    defaults = {
        "id": "f1",
        "storage_key": "chat/202608/aaa.pdf",
        "filename": "notes.pdf",
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


async def _seed_tracked(store, fid: str = "f1", *, article: str | None = "art-1") -> None:
    await store.save_scoped(_record(id=fid, filename=f"{fid}.pdf"), workspace="w1")
    if article:
        await store.set_kb_article(fid, "w1", article_id=article, scope="workspace:w1")


# --- (1) provenance stamp on ingest ---------------------------------------


async def test_ingest_source_carries_file_id(monkeypatch, store, tmp_path):
    """The kb-go --source string is "<filename>#file:<file_id>"."""
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), workspace="w1")
    fake_path = tmp_path / "notes.pdf"
    fake_path.write_bytes(b"unused; chain mocked")
    chain = _FakeChain(ExtractionResult(text="meeting notes", backend="local"))
    ingest = AsyncMock(return_value={"id": "art-1"})
    _wire(monkeypatch, chain=chain, adapter=_FakeAdapter(fake_path), ingest=ingest)

    await index_uploaded_file(
        FileReady(
            data={
                "workspace_id": "w1",
                "file_id": "f1",
                "filename": "notes.pdf",
                "mime": "application/pdf",
                "storage_key": "chat/202608/aaa.pdf",
            }
        )
    )

    ingest.assert_awaited_once_with(
        scope="workspace:w1",
        text="meeting notes",
        source="notes.pdf#file:f1",
    )


async def test_lang_hint_survives_file_id_stamp():
    """A stamped code-file source still resolves its --lang AST hint."""
    from pocketpaw_ee.cloud.agents.knowledge import _lang_for_source, source_with_file_id

    stamped = source_with_file_id("main.py", "f1")
    assert stamped == "main.py#file:f1"
    assert _lang_for_source(stamped) == "python"
    # Plain filenames keep working; non-code stays None either way.
    assert _lang_for_source("main.py") == "python"
    assert _lang_for_source(source_with_file_id("notes.pdf", "f1")) is None


# --- (2) FileDeleted subscriber purges the tracked article ----------------


async def test_prune_deletes_tracked_article_from_tombstoned_row(monkeypatch, store):
    """Subscriber purges via the persisted ids and clears the tracking.

    The row is soft-deleted BEFORE the event fires (service order), so this
    also proves the tombstone-inclusive read path.
    """
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads.listeners import prune_deleted_file_knowledge
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    await _seed_tracked(store, "f1")
    await store.soft_delete_scoped("f1", "w1")

    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)

    await prune_deleted_file_knowledge(FileDeleted(data={"workspace_id": "w1", "file_id": "f1"}))

    remove.assert_awaited_once_with("workspace:w1", "art-1")
    # Tracking cleared on the tombstone (mirrors the FL-11b purge contract).
    doc = await MongoFileStore().get_doc_scoped_any("f1", "w1")
    assert doc is not None
    assert doc.deleted_at is not None
    assert doc.kb_article_id is None
    assert doc.kb_scope is None


async def test_prune_workspace_scoped_lookup(monkeypatch, store):
    """Wrong workspace in the payload → no cross-tenant purge."""
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads.listeners import prune_deleted_file_knowledge

    await _seed_tracked(store, "f1")
    await store.soft_delete_scoped("f1", "w1")

    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)

    await prune_deleted_file_knowledge(FileDeleted(data={"workspace_id": "OTHER", "file_id": "f1"}))
    remove.assert_not_awaited()


# --- (3) safe no-ops ------------------------------------------------------


async def test_prune_no_tracked_article_is_noop(monkeypatch, store):
    """A deleted file that was never KB-indexed triggers no kb delete."""
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads.listeners import prune_deleted_file_knowledge

    await _seed_tracked(store, "f2", article=None)
    await store.soft_delete_scoped("f2", "w1")

    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)

    await prune_deleted_file_knowledge(FileDeleted(data={"workspace_id": "w1", "file_id": "f2"}))
    remove.assert_not_awaited()


async def test_prune_missing_payload_fields_is_noop(monkeypatch):
    """No workspace_id / file_id → quiet return, no store or kb calls."""
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads.listeners import prune_deleted_file_knowledge

    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)

    await prune_deleted_file_knowledge(FileDeleted(data={"file_id": "f1"}))
    await prune_deleted_file_knowledge(FileDeleted(data={"workspace_id": "w1"}))
    await prune_deleted_file_knowledge(FileDeleted(data=None))
    remove.assert_not_awaited()


async def test_prune_unresolvable_row_is_noop(monkeypatch, store):
    """No row at all for the id → quiet return."""
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads.listeners import prune_deleted_file_knowledge

    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)

    await prune_deleted_file_knowledge(FileDeleted(data={"workspace_id": "w1", "file_id": "ghost"}))
    remove.assert_not_awaited()


# --- (4) purge failure keeps tracking -------------------------------------


async def test_prune_failure_keeps_tracking(monkeypatch, store):
    """remove_article returning False leaves the ids for a sweeper re-purge."""
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads.listeners import prune_deleted_file_knowledge
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    await _seed_tracked(store, "f3", article="art-3")
    await store.soft_delete_scoped("f3", "w1")

    remove = AsyncMock(return_value=False)
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)

    await prune_deleted_file_knowledge(FileDeleted(data={"workspace_id": "w1", "file_id": "f3"}))

    remove.assert_awaited_once_with("workspace:w1", "art-3")
    doc = await MongoFileStore().get_doc_scoped_any("f3", "w1")
    assert doc.kb_article_id == "art-3"
    assert doc.kb_scope == "workspace:w1"


# --- (5) registration ------------------------------------------------------


async def test_register_upload_listeners_subscribes_to_file_deleted():
    """Bootstrap path: the prune handler is wired to FileDeleted.EVENT_TYPE."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
    from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus
    from pocketpaw_ee.cloud.uploads.listeners import (
        prune_deleted_file_knowledge,
        register_upload_listeners,
    )

    real_bus = InProcessBus(resolver=AudienceResolver(), conn_manager=AsyncMock())
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = real_bus  # type: ignore[attr-defined]
    try:
        register_upload_listeners()
        handlers = real_bus._handlers.get(FileDeleted.EVENT_TYPE, [])
        assert prune_deleted_file_knowledge in handlers
    finally:
        bus_mod._bus = prev  # type: ignore[attr-defined]


# --- store: tombstone-inclusive read/write --------------------------------


async def test_get_doc_scoped_any_sees_tombstones(store):
    await store.save_scoped(_record(id="f4"), workspace="w1")
    await store.soft_delete_scoped("f4", "w1")

    assert await store.get_doc_scoped("f4", "w1") is None
    doc = await store.get_doc_scoped_any("f4", "w1")
    assert doc is not None and doc.file_id == "f4"
    # Workspace filter still applies.
    assert await store.get_doc_scoped_any("f4", "w2") is None


async def test_set_kb_article_include_deleted(store):
    await store.save_scoped(_record(id="f5"), workspace="w1")
    await store.set_kb_article("f5", "w1", article_id="a5", scope="workspace:w1")
    await store.soft_delete_scoped("f5", "w1")

    # Default (live-only) semantics unchanged: tombstone not matched.
    assert await store.set_kb_article("f5", "w1", article_id=None, scope=None) is None
    # include_deleted reaches the tombstone.
    updated = await store.set_kb_article(
        "f5", "w1", article_id=None, scope=None, include_deleted=True
    )
    assert updated is not None
    doc = await store.get_doc_scoped_any("f5", "w1")
    assert doc.kb_article_id is None and doc.kb_scope is None
