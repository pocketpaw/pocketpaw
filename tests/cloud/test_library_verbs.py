# test_library_verbs.py — FL-3 agent Library verb tests (tag/move/annotate/search).
# Created: 2026-07-03 (FL-3). Locks:
#   - tag_file adds/removes tags (persists via FL-1, readable) + emits a journal event
#   - move_file moves a file into a folder + emits a journal event
#   - annotate_file writes a new FL-2 version (revertable via list_versions/get_version)
#   - search_library wraps the kb-go workspace scope and returns hits
#   - cross-workspace access is DENIED for every verb (tenant isolation)
# Workspace context is bound via the OSS ``pocketpaw.stores.current_workspace``
# ContextVar (the ISO-3 bridge ``attach_agent_identity`` sets), so the verbs
# resolve the active workspace without a request scope — the same seam the live
# agent path uses.
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.file_versions import service as fv_service
from pocketpaw_ee.cloud.uploads.models import FileFolder, FileUpload

from pocketpaw.tools.builtin.library_verbs import (
    AnnotateFileTool,
    MoveFileTool,
    SearchLibraryTool,
    TagFileTool,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _FakeAdapter:
    """In-memory StorageAdapter stand-in (put + open over a dict of blobs)."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def put(self, key: str, stream: AsyncIterator[bytes], mime: str):
        from pocketpaw.uploads.adapter import StoredObject

        chunks: list[bytes] = []
        async for chunk in stream:
            chunks.append(chunk)
        data = b"".join(chunks)
        self._blobs[key] = data
        return StoredObject(key=key, size=len(data), mime=mime)

    async def open(self, key: str) -> AsyncIterator[bytes]:
        data = self._blobs.get(key)
        if data is None:
            raise FileNotFoundError(key)
        yield data


@pytest.fixture
def fake_storage():
    """Inject an in-memory StorageAdapter into the file_versions service."""
    adapter = _FakeAdapter()
    prev = fv_service._adapter
    fv_service.set_adapter(adapter)
    yield adapter
    fv_service._adapter = prev


@contextmanager
def _workspace(workspace_id: str, user_id: str = "u1"):
    """Bind the OSS current_workspace ContextVar + patch the EE user getter.

    The verbs read the workspace from ``pocketpaw.stores.current_workspace``
    (set in production by ``attach_agent_identity``) and the user from
    ``agent_service.current_user_id``; here we set the OSS var directly and
    monkeypatch the user getter so no full chat stream is needed.
    """
    import pocketpaw.tools.builtin.library_verbs as lv
    from pocketpaw.stores import current_workspace

    tok = current_workspace.set(workspace_id)
    orig_ws = lv._current_workspace
    orig_user = lv._current_user
    lv._current_workspace = lambda: current_workspace.get()
    lv._current_user = lambda: user_id
    try:
        yield
    finally:
        lv._current_workspace = orig_ws
        lv._current_user = orig_user
        current_workspace.reset(tok)


async def _seed_upload(
    workspace: str,
    file_id: str,
    *,
    filename: str = "note.txt",
    mime: str = "text/plain",
    content: str = "hello world",
    adapter: _FakeAdapter,
    folder_path: str = "/",
) -> FileUpload:
    """Seed a Library FileUpload row + its blob, bypassing the guarded route."""
    storage_key = f"editor/{workspace}/{file_id}"
    await adapter.put(storage_key, _one_chunk(content.encode("utf-8")), mime)
    doc = FileUpload(
        file_id=file_id,
        storage_key=storage_key,
        filename=filename,
        mime=mime,
        size=len(content.encode("utf-8")),
        workspace=workspace,
        owner="u1",
        folder_path=folder_path,
        content_version=1,
    )
    await doc.insert()
    return doc


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


@pytest.fixture
def captured_events(monkeypatch):
    """Capture every Event emitted via the verbs' journal facade."""
    events: list = []

    async def _fake_emit(event_type, data):
        events.append((event_type, data))

    import pocketpaw.tools.builtin.library_verbs as lv

    monkeypatch.setattr(lv, "_emit_library_event", _fake_emit)
    return events


# ---------------------------------------------------------------------------
# tag_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tag_file_adds_and_persists(mongo_db, fake_storage, captured_events):
    await _seed_upload("w1", "f1", adapter=fake_storage)

    with _workspace("w1"):
        out = await TagFileTool().execute("f1", add=["invoice", "q3"])

    assert "invoice" in out and "q3" in out
    doc = await FileUpload.find_one({"file_id": "f1", "workspace": "w1"})
    assert set(doc.tags) == {"invoice", "q3"}
    # A journal event was emitted.
    assert captured_events[0][0] == "file.tagged"
    assert captured_events[0][1]["file_id"] == "f1"


@pytest.mark.asyncio
async def test_tag_file_removes(mongo_db, fake_storage, captured_events):
    doc = await _seed_upload("w1", "f1", adapter=fake_storage)
    doc.tags = ["keep", "drop"]
    await doc.save()

    with _workspace("w1"):
        await TagFileTool().execute("f1", remove=["drop"])

    fresh = await FileUpload.find_one({"file_id": "f1", "workspace": "w1"})
    assert fresh.tags == ["keep"]


@pytest.mark.asyncio
async def test_tag_file_cross_workspace_denied(mongo_db, fake_storage, captured_events):
    await _seed_upload("wA", "fa", adapter=fake_storage)

    # A caller in wB cannot tag wA's file.
    with _workspace("wB"):
        out = await TagFileTool().execute("fa", add=["x"])

    assert "not found" in out.lower()
    doc = await FileUpload.find_one({"file_id": "fa", "workspace": "wA"})
    assert doc.tags == []  # untouched
    assert captured_events == []  # no event on a denied op


# ---------------------------------------------------------------------------
# move_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_move_file_into_folder(mongo_db, fake_storage, captured_events):
    await _seed_upload("w1", "f1", adapter=fake_storage)
    # The destination folder must exist (the verb validates via FolderStore).
    await FileFolder(workspace="w1", owner="u1", path="/reports", name="reports").insert()

    with _workspace("w1"):
        out = await MoveFileTool().execute("f1", "/reports")

    assert "/reports" in out
    doc = await FileUpload.find_one({"file_id": "f1", "workspace": "w1"})
    assert doc.folder_path == "/reports"
    assert captured_events[0][0] == "file.moved"
    assert captured_events[0][1]["folder_path"] == "/reports"


@pytest.mark.asyncio
async def test_move_file_missing_folder_rejected(mongo_db, fake_storage, captured_events):
    await _seed_upload("w1", "f1", adapter=fake_storage)

    with _workspace("w1"):
        out = await MoveFileTool().execute("f1", "/does-not-exist")

    assert "does not exist" in out.lower()
    doc = await FileUpload.find_one({"file_id": "f1", "workspace": "w1"})
    assert (doc.folder_path or "/") == "/"


@pytest.mark.asyncio
async def test_move_file_cross_workspace_denied(mongo_db, fake_storage, captured_events):
    await _seed_upload("wA", "fa", adapter=fake_storage)

    with _workspace("wB"):
        out = await MoveFileTool().execute("fa", "/")

    assert "not found" in out.lower()


# ---------------------------------------------------------------------------
# annotate_file
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_writes_revertable_version(mongo_db, fake_storage):
    await _seed_upload("w1", "f1", content="original body", adapter=fake_storage)

    with _workspace("w1"):
        out = await AnnotateFileTool().execute("f1", "one-line summary")

    assert "version" in out.lower()

    # A new FileVersionDoc was archived (the prior content) — revertable.
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    ctx = RequestContext(
        user_id="u1",
        workspace_id="w1",
        request_id="",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )
    versions = await fv_service.list_versions(ctx, "f1")
    assert len(versions) == 1
    archived = await fv_service.get_version(ctx, "f1", versions[0].id)
    assert archived.content == "original body"  # the pre-annotation content is recoverable

    # The live blob now carries the note prepended + bumped content_version.
    doc = await FileUpload.find_one({"file_id": "f1", "workspace": "w1"})
    assert doc.content_version == 2
    live = fake_storage._blobs[doc.storage_key].decode()
    assert live.startswith("[note] one-line summary")
    assert "original body" in live


@pytest.mark.asyncio
async def test_annotate_non_text_rejected(mongo_db, fake_storage):
    # Genuinely non-editable: image mime AND a non-text extension (the
    # editability gate is extension-aware now — a default .txt filename would
    # make it editable, which is not what this test means to assert).
    await _seed_upload(
        "w1", "img", filename="photo.png", mime="image/png", content="x", adapter=fake_storage
    )

    with _workspace("w1"):
        out = await AnnotateFileTool().execute("img", "caption")

    assert "cannot be annotated" in out.lower() or "not_editable" in out.lower()


@pytest.mark.asyncio
async def test_annotate_code_file_by_extension(mongo_db, fake_storage):
    # CR-1: upload mimes are unreliable — a .go/.toml/.svg often arrives as
    # application/octet-stream. The editability gate is extension-aware, so the
    # file is editable (aligned with the frontend's Edit-tab affordance) instead
    # of 422-ing on save.
    await _seed_upload(
        "w1",
        "code",
        filename="main.go",
        mime="application/octet-stream",
        content="package main",
        adapter=fake_storage,
    )
    with _workspace("w1"):
        out = await AnnotateFileTool().execute("code", "TODO: refactor")

    assert "not_editable" not in out.lower() and "cannot be annotated" not in out.lower()
    doc = await FileUpload.find_one({"file_id": "code", "workspace": "w1"})
    assert doc.content_version == 2  # a revertable version was written


@pytest.mark.asyncio
async def test_annotate_cross_workspace_denied(mongo_db, fake_storage):
    await _seed_upload("wA", "fa", content="secret", adapter=fake_storage)

    with _workspace("wB"):
        out = await AnnotateFileTool().execute("fa", "note")

    assert "not found" in out.lower()
    # No version was written for wA's file by the wB caller.
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    ctx = RequestContext(
        user_id="ua",
        workspace_id="wA",
        request_id="",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )
    assert await fv_service.list_versions(ctx, "fa") == []
    doc = await FileUpload.find_one({"file_id": "fa", "workspace": "wA"})
    assert doc.content_version == 1  # unchanged


# ---------------------------------------------------------------------------
# search_library
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_library_scopes_to_workspace(mongo_db, fake_storage, monkeypatch):
    calls: list = []

    async def _fake_search(*, scope, query, limit):
        calls.append((scope, query, limit))
        return f"[hit] whiteboard photo from the offsite (scope={scope})"

    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    monkeypatch.setattr(KnowledgeService, "search_context_for_scope", _fake_search)

    with _workspace("w1"):
        out = await SearchLibraryTool().execute("whiteboard", limit=3)

    assert "whiteboard" in out
    # The verb scoped the search to THIS workspace's KB scope.
    assert calls == [("workspace:w1", "whiteboard", 3)]


@pytest.mark.asyncio
async def test_search_library_cross_workspace_scope_isolated(mongo_db, fake_storage, monkeypatch):
    seen_scopes: list[str] = []

    async def _fake_search(*, scope, query, limit):
        seen_scopes.append(scope)
        return ""

    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    monkeypatch.setattr(KnowledgeService, "search_context_for_scope", _fake_search)

    with _workspace("wB"):
        await SearchLibraryTool().execute("anything")

    # A wB caller can only ever query wB's KB scope — never wA's.
    assert seen_scopes == ["workspace:wB"]


@pytest.mark.asyncio
async def test_verbs_require_workspace_context(mongo_db, fake_storage):
    # With no workspace bound, every verb refuses (fail-closed).
    import pocketpaw.tools.builtin.library_verbs as lv

    orig = lv._current_workspace
    lv._current_workspace = lambda: None
    try:
        assert "workspace context" in (await TagFileTool().execute("f", add=["x"])).lower()
        assert "workspace context" in (await MoveFileTool().execute("f", "/")).lower()
        assert "workspace context" in (await AnnotateFileTool().execute("f", "n")).lower()
        assert "workspace context" in (await SearchLibraryTool().execute("q")).lower()
    finally:
        lv._current_workspace = orig
