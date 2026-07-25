# test_codeproject_bulk_files.py — the in-tab whole-project write.
#
# Created 2026-07-25 (S1 follow-up). `put_project_files` is the in-tab analog of
# the VM's `sync_project_files`: the browser hosts the filesystem, so it is the
# only thing that can enumerate one, and it posts the whole map at once.
#
# What these lock is the property that makes the restore-time PRUNE safe to run:
# `overlay_complete` may only ever be set by a write that WHOLLY succeeded. A
# store marked complete after a partial upload would license deleting exactly the
# files that failed to upload — so the all-or-nothing test below is the important
# one, not the happy path.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError, NotFound, ValidationError
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.websandbox import durability

from tests.cloud.test_codeproject_file_sync import _FakeUploads, _project

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "w1"
_USER = "u1"


async def test_a_whole_project_write_marks_the_store_complete() -> None:
    """The happy path, and the flag that licenses the prune."""
    project = await _project()
    uploads = _FakeUploads()

    stored = await durability.put_project_files(
        _WS,
        _USER,
        project.id,
        {"src/App.tsx": "export default App;", "README.md": "# hi"},
        uploads=uploads,
    )

    assert stored == 2
    view = await codeproject_service.get_project(_WS, _USER, project.id)
    assert set(view.overlay) == {"src/App.tsx", "README.md"}
    assert view.overlay_complete is True


async def test_a_replacement_drops_a_path_the_client_no_longer_has() -> None:
    """The delete that used to come back. The map is the project, so a path the
    client didn't send is a path the project doesn't have."""
    project = await _project()
    uploads = _FakeUploads()

    await durability.put_project_files(
        _WS, _USER, project.id, {"keep.ts": "a", "delete-me.ts": "b"}, uploads=uploads
    )
    await durability.put_project_files(_WS, _USER, project.id, {"keep.ts": "a"}, uploads=uploads)

    view = await codeproject_service.get_project(_WS, _USER, project.id)
    assert set(view.overlay) == {"keep.ts"}


async def test_a_failed_upload_leaves_the_store_untouched_and_incomplete() -> None:
    """THE load-bearing test. A partial upload must not become a complete store,
    because `overlay_complete` is what lets the restore prune delete files."""
    project = await _project()
    uploads = _FakeUploads()

    # Seed a known-good prior state so we can prove it survives the failure.
    await durability.put_project_files(_WS, _USER, project.id, {"before.ts": "x"}, uploads=uploads)
    await codeproject_service.replace_project_overlay(
        _WS, _USER, project.id, {"before.ts": "file-1"}, complete=False
    )

    calls = {"n": 0}
    original = uploads.upload

    async def _fail_on_second(*args, **kwargs):  # noqa: ANN002, ANN003
        calls["n"] += 1
        if calls["n"] >= 2:
            raise RuntimeError("storage went away mid-write")
        return await original(*args, **kwargs)

    uploads.upload = _fail_on_second  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await durability.put_project_files(
            _WS, _USER, project.id, {"a.ts": "1", "b.ts": "2", "c.ts": "3"}, uploads=uploads
        )

    view = await codeproject_service.get_project(_WS, _USER, project.id)
    assert set(view.overlay) == {"before.ts"}, "a failed write must not swap the map"
    assert view.overlay_complete is False, "a partial upload must never read as complete"


async def test_an_escaping_path_is_rejected_before_anything_uploads() -> None:
    """Jailed at the boundary, and jailed BEFORE the first upload — a rejected
    batch should not leave orphan blobs behind."""
    project = await _project()
    uploads = _FakeUploads()

    with pytest.raises(ValidationError):
        await durability.put_project_files(
            _WS, _USER, project.id, {"ok.ts": "a", "../escape.ts": "b"}, uploads=uploads
        )

    assert uploads.upload_calls == []
    view = await codeproject_service.get_project(_WS, _USER, project.id)
    assert view.overlay == {}


async def test_over_the_file_count_cap_fails_loud(monkeypatch) -> None:  # noqa: ANN001
    """Loud, not truncated — a silently short store is the input to a prune."""
    project = await _project()
    uploads = _FakeUploads()
    monkeypatch.setattr(durability, "_overlay_max_files", lambda: 2)

    with pytest.raises(CloudError) as err:
        await durability.put_project_files(
            _WS, _USER, project.id, {"a": "1", "b": "2", "c": "3"}, uploads=uploads
        )

    assert err.value.code == "codeproject.overlay_too_many_files"
    assert uploads.upload_calls == []


async def test_a_foreign_caller_cannot_replace_the_store() -> None:
    """Rejected, and rejected BEFORE spending storage — an unauthorized request
    should not be able to upload its way to a 404."""
    project = await _project()
    uploads = _FakeUploads()

    with pytest.raises(NotFound):
        await durability.put_project_files(
            "other-ws", _USER, project.id, {"a.ts": "1"}, uploads=uploads
        )
    with pytest.raises(NotFound):
        await durability.put_project_files(
            _WS, "other-user", project.id, {"a.ts": "1"}, uploads=uploads
        )

    assert uploads.upload_calls == [], "a foreign caller must not reach the upload path"
