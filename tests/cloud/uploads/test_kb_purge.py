# test_kb_purge.py — FL-11b retroactive KB-purge tests.
# Created: 2026-07-03 — FL-11b "hide-from-AI purge". Covers the full slice:
#   (1) a successful ingest records kb_article_id + kb_scope on the FileUpload
#       row (listener → store).
#   (2) PATCH hide_from_ai false→true on a TRACKED file calls
#       KnowledgeService.remove_article with the tracked id + scope and clears
#       the tracking fields (router → service, mocked _kb).
#   (3) PATCH on an UNTRACKED file (no article) triggers no kb delete.
#   (4) A PATCH that doesn't change hide_from_ai, or a file already hidden,
#       triggers no purge.
#   (5) The store setter round-trips + clears; remove_article maps to `kb
#       delete` and swallows subprocess errors.
# Uses the cloud/uploads conftest fixtures (beanie_upload_db + store) and the
# ee_client router harness pattern (seed via the store since guarded POST 401s
# in this env — a pre-existing harness gap, not our regression).
"""Integration tests for the FL-11b hide-from-AI KB purge path."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core.realtime.events import FileReady
from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult

from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.asyncio


# --- listener wiring (mirrors test_listener_auto_tag) ---------------------


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


async def test_ingest_records_kb_article_and_scope(monkeypatch, store, tmp_path):
    """A successful ingest persists kb_article_id + kb_scope on the row."""
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    await store.save_scoped(_record(), workspace="w1")

    fake_path = tmp_path / "invoice.pdf"
    fake_path.write_bytes(b"unused; chain mocked")
    chain = _FakeChain(
        ExtractionResult(title="Invoice", text="payable amount total due", backend="local")
    )
    ingest = AsyncMock(return_value={"id": "art-42"})
    _wire(monkeypatch, chain=chain, adapter=_FakeAdapter(fake_path), ingest=ingest)

    await index_uploaded_file(_event())

    doc = await store.get_doc_scoped("f1", "w1")
    assert doc is not None
    assert doc.kb_article_id == "art-42"
    # Workspace-scoped upload → workspace:{wid} scope recorded.
    assert doc.kb_scope == "workspace:w1"
    ingest.assert_awaited_once()


async def test_ingest_records_pocket_scope(monkeypatch, store, tmp_path):
    """A pocket-scoped upload records the pocket:{id} scope, not workspace."""
    from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

    rec = _record(id="f2")
    await store.save_scoped(rec, workspace="w1", pocket_id="p9")

    fake_path = tmp_path / "invoice.pdf"
    fake_path.write_bytes(b"x")
    chain = _FakeChain(ExtractionResult(text="content here", backend="local"))
    ingest = AsyncMock(return_value={"id": "art-9"})
    _wire(monkeypatch, chain=chain, adapter=_FakeAdapter(fake_path), ingest=ingest)

    ev = FileReady(
        data={
            "workspace_id": "w1",
            "pocket_id": "p9",
            "file_id": "f2",
            "filename": "invoice.pdf",
            "mime": "application/pdf",
            "storage_key": "chat/202607/aaa.pdf",
        }
    )
    await index_uploaded_file(ev)

    doc = await store.get_doc_scoped("f2", "w1")
    assert doc is not None
    assert doc.kb_article_id == "art-9"
    assert doc.kb_scope == "pocket:p9"


# --- store setter --------------------------------------------------------


async def test_set_kb_article_roundtrips_and_clears(store):
    await store.save_scoped(_record(id="f3"), workspace="w1")

    updated = await store.set_kb_article("f3", "w1", article_id="a1", scope="workspace:w1")
    assert updated is not None
    doc = await store.get_doc_scoped("f3", "w1")
    assert doc.kb_article_id == "a1"
    assert doc.kb_scope == "workspace:w1"

    # Clearing to None round-trips.
    await store.set_kb_article("f3", "w1", article_id=None, scope=None)
    doc = await store.get_doc_scoped("f3", "w1")
    assert doc.kb_article_id is None
    assert doc.kb_scope is None


async def test_set_kb_article_workspace_scoped(store):
    """Cannot set tracking on another tenant's row."""
    await store.save_scoped(_record(id="f4"), workspace="w1")
    # Wrong workspace → no row matched → None, and w1's row untouched.
    result = await store.set_kb_article("f4", "w2", article_id="a1", scope="s")
    assert result is None
    doc = await store.get_doc_scoped("f4", "w1")
    assert doc.kb_article_id is None


# --- service remove_article ---------------------------------------------


async def test_remove_article_calls_kb_delete(monkeypatch):
    from pocketpaw_ee.cloud.agents import knowledge as kn

    calls: list[tuple] = []

    def _fake_kb(*args, **kwargs):
        calls.append(args)
        return {}

    monkeypatch.setattr(kn, "_kb", _fake_kb)

    ok = await kn.KnowledgeService.remove_article("workspace:w1", "art-1")
    assert ok is True
    assert calls == [("delete", "art-1", "--scope", "workspace:w1")]


async def test_remove_article_swallows_errors(monkeypatch):
    from pocketpaw_ee.cloud.agents import knowledge as kn

    def _boom(*args, **kwargs):
        raise RuntimeError("kb delete failed: boom")

    monkeypatch.setattr(kn, "_kb", _boom)

    # Resilient: returns False, does not raise.
    ok = await kn.KnowledgeService.remove_article("workspace:w1", "art-1")
    assert ok is False


# --- PATCH route purge trigger ------------------------------------------


@pytest.fixture()
def ee_client(tmp_path: Path, beanie_upload_db, monkeypatch):
    """EE uploads router with license + identity deps overridden."""
    import pocketpaw_ee.cloud.uploads.router as uploads_module
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore
    from pocketpaw_ee.cloud.uploads.service import EEUploadService

    from pocketpaw.uploads.config import UploadSettings
    from pocketpaw.uploads.local import LocalStorageAdapter

    root = tmp_path / "u"
    root.mkdir()
    test_cfg = UploadSettings(local_root=root)
    test_adapter = LocalStorageAdapter(root=root)
    test_meta = MongoFileStore()
    test_svc = EEUploadService(adapter=test_adapter, meta=test_meta, cfg=test_cfg)
    monkeypatch.setattr(uploads_module, "_SVC", test_svc)

    app = FastAPI()
    from fastapi import Header
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    app.dependency_overrides[require_license] = lambda: None

    async def _user_dep(x_user: str = Header(default="u1")) -> str:
        return x_user

    async def _workspace_dep(x_workspace: str = Header(default="w1")) -> str:
        return x_workspace

    app.dependency_overrides[current_user_id] = _user_dep
    app.dependency_overrides[current_workspace_id] = _workspace_dep
    app.include_router(uploads_module.router, prefix="/api/v1")
    return TestClient(app)


async def _seed(fid: str, *, ws: str = "w1", user: str = "u1", **meta) -> None:
    """Seed a live upload row directly (guarded POST 401s in this env)."""
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    rec = FileRecord(
        id=fid,
        storage_key=f"chat/202607/{fid}.png",
        filename=f"{fid}.png",
        mime="image/png",
        size=4,
        owner_id=user,
        chat_id=None,
        created=datetime.now(UTC),
    )
    s = MongoFileStore()
    await s.save_scoped(rec, workspace=ws)
    if "article_id" in meta or "scope" in meta:
        await s.set_kb_article(fid, ws, article_id=meta.get("article_id"), scope=meta.get("scope"))


async def test_patch_hide_purges_tracked_article(ee_client, monkeypatch):
    """false→true on a tracked file → remove_article(id, scope) + tracking cleared."""
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    await _seed("p1", article_id="art-7", scope="workspace:w1")

    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)

    r = ee_client.patch(
        "/api/v1/uploads/p1",
        json={"hide_from_ai": True},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["hide_from_ai"] is True

    remove.assert_awaited_once_with("workspace:w1", "art-7")

    # Tracking cleared so a re-index re-tracks.
    doc = await MongoFileStore().get_doc_scoped("p1", "w1")
    assert doc.hide_from_ai is True
    assert doc.kb_article_id is None
    assert doc.kb_scope is None


async def test_patch_hide_untracked_file_no_purge(ee_client, monkeypatch):
    """A file with no tracked article → no kb delete call."""
    from pocketpaw_ee.cloud.agents import knowledge as kn

    await _seed("p2")  # no article tracked

    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)

    r = ee_client.patch(
        "/api/v1/uploads/p2",
        json={"hide_from_ai": True},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 200, r.text
    remove.assert_not_awaited()


async def test_patch_already_hidden_no_purge(ee_client, monkeypatch):
    """A file already hidden (true→true) → no purge even if tracked."""
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    await _seed("p3", article_id="art-x", scope="workspace:w1")
    await MongoFileStore().set_library_metadata("p3", "w1", hide_from_ai=True)

    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)

    r = ee_client.patch(
        "/api/v1/uploads/p3",
        json={"hide_from_ai": True},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 200, r.text
    remove.assert_not_awaited()


async def test_patch_no_hide_change_no_purge(ee_client, monkeypatch):
    """A PATCH that doesn't touch hide_from_ai → no purge."""
    from pocketpaw_ee.cloud.agents import knowledge as kn

    await _seed("p4", article_id="art-y", scope="workspace:w1")

    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)

    r = ee_client.patch(
        "/api/v1/uploads/p4",
        json={"tags": ["invoice"]},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 200, r.text
    remove.assert_not_awaited()


async def test_patch_unhide_true_to_false_no_purge(ee_client, monkeypatch):
    """Un-hiding (true→false) is not a purge trigger."""
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    await _seed("p5", article_id="art-z", scope="workspace:w1")
    await MongoFileStore().set_library_metadata("p5", "w1", hide_from_ai=True)

    remove = AsyncMock(return_value=True)
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)

    r = ee_client.patch(
        "/api/v1/uploads/p5",
        json={"hide_from_ai": False},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 200, r.text
    remove.assert_not_awaited()


async def test_patch_purge_failure_still_hides(ee_client, monkeypatch):
    """A purge failure logs but the hide flag still applies; tracking kept."""
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    await _seed("p6", article_id="art-q", scope="workspace:w1")

    remove = AsyncMock(return_value=False)  # purge failed
    monkeypatch.setattr(kn.KnowledgeService, "remove_article", remove)

    r = ee_client.patch(
        "/api/v1/uploads/p6",
        json={"hide_from_ai": True},
        headers={"x-user": "u1", "x-workspace": "w1"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["hide_from_ai"] is True
    remove.assert_awaited_once()

    # Hide applied; tracking NOT cleared (so a sweeper can re-purge).
    doc = await MongoFileStore().get_doc_scoped("p6", "w1")
    assert doc.hide_from_ai is True
    assert doc.kb_article_id == "art-q"
