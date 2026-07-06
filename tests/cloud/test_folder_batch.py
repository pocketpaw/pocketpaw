# test_folder_batch.py — FL-4 organize_folder (folder-batch executor) tests.
# Created: 2026-07-03 (FL-4). Locks the folder-batch executor behaviour:
#   - batch-annotate over a small folder writes a revertable FL-2 version per
#     file and returns a per-file summary (the Poly headline demo)
#   - batch-tag adds tags to every file in the folder
#   - a folder exceeding max_files returns a clear over-cap message and runs
#     NOTHING (no silent truncation)
#   - a non-admin scope cannot raise max_files above the default cap
#   - one file failing (a binary file for 'annotate') does NOT abort the batch —
#     the rest still process and the failure is reported per-file
#   - the sweep is workspace-scoped: files in another workspace / another folder
#     are never touched
# Reuses the FL-3 test seam: the workspace is bound via the OSS
# ``pocketpaw.stores.current_workspace`` ContextVar and the EE user getter is
# monkeypatched, so no full chat stream is needed. The verb reuses the FL-3
# AnnotateFileTool / TagFileTool internals, so a passing FL-3 suite underpins
# these batch assertions.
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import contextmanager

import pytest
from pocketpaw_ee.cloud.file_versions import service as fv_service
from pocketpaw_ee.cloud.uploads.models import FileUpload

from pocketpaw.tools.builtin.library_verbs import OrganizeFolderTool

# ---------------------------------------------------------------------------
# Helpers / fixtures (mirrors tests/cloud/test_library_verbs.py)
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


async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
    yield data


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


@contextmanager
def _workspace(workspace_id: str, user_id: str = "u1", *, is_admin: bool = False):
    """Bind the OSS current_workspace ContextVar + patch the EE getters.

    The verb reads the workspace from ``pocketpaw.stores.current_workspace``,
    the user from ``_current_user``, and the admin flag from ``_current_is_admin``.
    We set the OSS var and monkeypatch the two getters so no chat stream is needed.
    """
    import pocketpaw.tools.builtin.library_verbs as lv
    from pocketpaw.stores import current_workspace

    tok = current_workspace.set(workspace_id)
    orig_ws = lv._current_workspace
    orig_user = lv._current_user
    orig_admin = lv._current_is_admin
    lv._current_workspace = lambda: current_workspace.get()
    lv._current_user = lambda: user_id
    lv._current_is_admin = lambda: is_admin
    try:
        yield
    finally:
        lv._current_workspace = orig_ws
        lv._current_user = orig_user
        lv._current_is_admin = orig_admin
        current_workspace.reset(tok)


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
# batch annotate — the headline demo
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_annotate_writes_a_version_per_file(mongo_db, fake_storage, captured_events):
    # Four text files in /reports, plus one decoy in the root that must be untouched.
    for i in range(4):
        await _seed_upload(
            "w1", f"r{i}", filename=f"r{i}.txt", adapter=fake_storage, folder_path="/reports"
        )
    await _seed_upload("w1", "root1", filename="root.txt", adapter=fake_storage, folder_path="/")

    with _workspace("w1"):
        out = await OrganizeFolderTool().execute("/reports", "annotate", note="one-line summary")

    assert "4 succeeded, 0 failed" in out

    # Each in-folder file got a new revertable version (content_version bumped 1 -> 2).
    for i in range(4):
        doc = await FileUpload.find_one({"file_id": f"r{i}", "workspace": "w1"})
        assert doc.content_version == 2
        versions = await fv_service.list_versions(_ctx("w1"), f"r{i}")
        assert len(versions) >= 1  # the pre-annotation blob is archived

    # The decoy outside the folder is untouched.
    decoy = await FileUpload.find_one({"file_id": "root1", "workspace": "w1"})
    assert decoy.content_version == 1

    # A single batch event summarizes the run.
    assert captured_events[-1][0] == "folder.batch"
    assert captured_events[-1][1]["ok"] == 4
    assert captured_events[-1][1]["failed"] == 0


def _ctx(workspace: str):
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    return RequestContext(
        user_id="u1",
        workspace_id=workspace,
        request_id="",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_batch_tag_adds_to_every_file(mongo_db, fake_storage, captured_events):
    for i in range(3):
        await _seed_upload(
            "w1", f"t{i}", filename=f"t{i}.txt", adapter=fake_storage, folder_path="/inbox"
        )

    with _workspace("w1"):
        out = await OrganizeFolderTool().execute("/inbox", "tag", add=["reviewed"])

    assert "3 succeeded, 0 failed" in out
    for i in range(3):
        doc = await FileUpload.find_one({"file_id": f"t{i}", "workspace": "w1"})
        assert "reviewed" in (doc.tags or [])


# ---------------------------------------------------------------------------
# cap — over-cap refuses, no silent truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_cap_refuses_without_truncation(mongo_db, fake_storage, captured_events):
    # Seed 5 files; force a cap of 3 (a normal scope may LOWER the cap freely).
    for i in range(5):
        await _seed_upload(
            "w1", f"c{i}", filename=f"c{i}.txt", adapter=fake_storage, folder_path="/big"
        )

    with _workspace("w1"):
        out = await OrganizeFolderTool().execute("/big", "tag", add=["x"], max_files=3)

    lowered = out.lower()
    assert "exceeds" in lowered or "more than" in lowered
    assert "3" in out  # names the cap

    # Nothing was tagged — the batch did not run at all.
    for i in range(5):
        doc = await FileUpload.find_one({"file_id": f"c{i}", "workspace": "w1"})
        assert doc.tags == []
    # No batch-completion event was emitted for the refused run.
    assert all(evt[0] != "folder.batch" for evt in captured_events)


@pytest.mark.asyncio
async def test_non_admin_cannot_raise_cap(mongo_db, fake_storage, captured_events):
    with _workspace("w1", is_admin=False):
        out = await OrganizeFolderTool().execute("/x", "tag", add=["y"], max_files=500)
    assert "admin" in out.lower()
    assert "500" in out


@pytest.mark.asyncio
async def test_admin_may_raise_cap(mongo_db, fake_storage, captured_events):
    # With admin scope, a max_files above the default is accepted (empty folder ⇒
    # a clean "nothing to do", proving the cap check did not reject the request).
    with _workspace("w1", is_admin=True):
        out = await OrganizeFolderTool().execute("/empty", "tag", add=["y"], max_files=500)
    assert "no files" in out.lower()


# ---------------------------------------------------------------------------
# partial-failure isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_failure_does_not_abort_batch(mongo_db, fake_storage, captured_events):
    # Two annotatable text files + one binary file (annotate → 422 not_editable).
    await _seed_upload("w1", "ok1", filename="a.txt", adapter=fake_storage, folder_path="/mix")
    await _seed_upload(
        "w1",
        "bad",
        filename="pic.png",
        mime="image/png",
        content="\x89PNGbinary",
        adapter=fake_storage,
        folder_path="/mix",
    )
    await _seed_upload("w1", "ok2", filename="b.txt", adapter=fake_storage, folder_path="/mix")

    with _workspace("w1"):
        out = await OrganizeFolderTool().execute("/mix", "annotate", note="summary")

    # 2 of 3 succeeded; the binary file is reported as failed, not fatal.
    assert "2 succeeded, 1 failed" in out
    assert "pic.png" in out

    # Both text files still got their new version despite the middle file failing.
    for fid in ("ok1", "ok2"):
        doc = await FileUpload.find_one({"file_id": fid, "workspace": "w1"})
        assert doc.content_version == 2
    # The binary file was left at its original version.
    bad = await FileUpload.find_one({"file_id": "bad", "workspace": "w1"})
    assert bad.content_version == 1

    assert captured_events[-1][1]["ok"] == 2
    assert captured_events[-1][1]["failed"] == 1


# ---------------------------------------------------------------------------
# workspace isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_workspace_files_not_touched(mongo_db, fake_storage, captured_events):
    # Same folder path, two workspaces. A batch in wA must never touch wB's file.
    await _seed_upload("wA", "a1", filename="a.txt", adapter=fake_storage, folder_path="/shared")
    await _seed_upload("wB", "b1", filename="b.txt", adapter=fake_storage, folder_path="/shared")

    with _workspace("wA"):
        out = await OrganizeFolderTool().execute("/shared", "tag", add=["done"])

    assert "1 succeeded" in out
    a = await FileUpload.find_one({"file_id": "a1", "workspace": "wA"})
    assert "done" in (a.tags or [])
    b = await FileUpload.find_one({"file_id": "b1", "workspace": "wB"})
    assert b.tags == []  # untouched


# ---------------------------------------------------------------------------
# arg validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_annotate_requires_note(mongo_db, fake_storage):
    with _workspace("w1"):
        out = await OrganizeFolderTool().execute("/x", "annotate")
    assert "note" in out.lower()


@pytest.mark.asyncio
async def test_tag_requires_a_tag(mongo_db, fake_storage):
    with _workspace("w1"):
        out = await OrganizeFolderTool().execute("/x", "tag")
    assert "tag" in out.lower()


@pytest.mark.asyncio
async def test_bad_action_rejected(mongo_db, fake_storage):
    with _workspace("w1"):
        out = await OrganizeFolderTool().execute("/x", "delete")
    assert "annotate" in out.lower() and "tag" in out.lower()
