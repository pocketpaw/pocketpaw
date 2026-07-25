# test_codeproject_file_sync.py — unit tests for BROWSER-runtime project file sync
# (B1, feat/code-project-file-sync): the in-tab (WebContainer) runtime pushes each
# write through to the project's durable overlay and pulls the whole set back on a
# reopen, because the filesystem lives in the user's browser and no backend hook
# can read it.
#
# Created 2026-07-25 (feat/code-project-file-sync).
#
# The bug these guard: an edit in a tab-hosted project was written to an in-tab
# filesystem only, so reopening the project re-materialized the bare starter
# scaffold and every change was gone.
#
# Blob storage is a FAKE injected through the ``uploads=`` DI seam — no test
# touches real S3. The CodeProject registry runs on real Beanie over
# mongomock-motor (the ``mongo_db`` fixture) so the owner-scoped overlay writes and
# the tenant filter are exercised for real. No Daytona client appears anywhere:
# that's the point — this path never has a VM.
#
# Covers:
#   * write-through: PUT records ``path -> file_id``; a second write to the same
#     path REPLACES the entry rather than duplicating it.
#   * round-trip: two files (one nested) come back with byte-identical content.
#   * delete: a dropped file no longer appears in the read-back.
#   * tenancy: another workspace / another user gets NotFound on every verb.
#   * the S3 fail-closed guard still raises on the write path in cloud.
#   * path jailing, the read-back caps, and the router wiring for all three routes.
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import CloudError, NotFound, ValidationError
from pocketpaw_ee.cloud.codeproject import router as codeproject_router
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.models.web_sandbox import WebSandbox as _WebSandboxDoc
from pocketpaw_ee.cloud.websandbox import durability

from pocketpaw.uploads.errors import NotFound as UploadNotFound
from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "w1"
_USER = "u1"


class _FakeUploads:
    """In-memory stand-in for EEUploadService — records uploads, serves them back.

    One instance carries the blobs, so a file written through here is readable by
    a later read-back in the same test (exactly the reopen the feature exists for).
    """

    def __init__(self) -> None:
        self.upload_calls: list[dict] = []
        self.stream_calls: list[dict] = []
        self._blobs: dict[str, bytes] = {}
        self._counter = 0

    async def upload(self, file, owner_id, chat_id, workspace, folder_path="/", pocket_id=None):  # noqa: ANN001
        data = await file.read()
        self._counter += 1
        file_id = f"file-{self._counter}"
        self.upload_calls.append(
            {
                "owner_id": owner_id,
                "workspace": workspace,
                "folder_path": folder_path,
                "filename": file.filename,
                "content_type": file.content_type,
                "data": data,
            }
        )
        self._blobs[file_id] = data
        return FileRecord(
            id=file_id,
            storage_key=f"key/{file_id}",
            filename=file.filename,
            mime="application/octet-stream",
            size=len(data),
            owner_id=owner_id,
            chat_id=None,
            created=datetime.now(UTC),
        )

    async def stream(self, file_id, requester_id, workspace):  # noqa: ANN001
        self.stream_calls.append({"file_id": file_id, "requester": requester_id, "ws": workspace})
        if file_id not in self._blobs:
            raise UploadNotFound()
        data = self._blobs[file_id]
        rec = FileRecord(
            id=file_id,
            storage_key=f"key/{file_id}",
            filename="overlay",
            mime="application/octet-stream",
            size=len(data),
            owner_id=requester_id,
            chat_id=None,
            created=datetime.now(UTC),
        )

        async def _iter():
            yield data

        return rec, _iter()


async def _project(workspace=_WS, user=_USER, name=None):  # noqa: ANN001
    """A durable starter project — the in-tab runtime's case (no repo to clone)."""
    body = {"repo": "react", "provider": "starter"}
    if name is not None:
        body["name"] = name
    return await codeproject_service.create_project(workspace, user, body)


def _ctx(workspace=_WS, user=_USER):  # noqa: ANN001
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="req-1",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Write-through — the overlay records the pointer, and a rewrite replaces it.
# ---------------------------------------------------------------------------


async def test_put_records_overlay_pointer() -> None:
    project = await _project()
    uploads = _FakeUploads()

    path, file_id = await durability.put_project_file(
        _WS, _USER, project.id, "src/app.ts", "console.log(1)", uploads=uploads
    )

    assert (path, file_id) == ("src/app.ts", "file-1")
    # The bytes landed in the tenant's blob storage, workspace + owner scoped.
    assert len(uploads.upload_calls) == 1
    up = uploads.upload_calls[0]
    assert up["workspace"] == _WS
    assert up["owner_id"] == _USER
    assert up["folder_path"] == "/code-project-overlay"
    assert up["data"] == b"console.log(1)"
    # …and the durable pointer is on the PROJECT row.
    assert (await codeproject_service.get_project(_WS, _USER, project.id)).overlay == {
        "src/app.ts": "file-1"
    }
    # No VM, no WebSandbox row — this runtime lives in the browser.
    assert await _WebSandboxDoc.find_all().to_list() == []


async def test_second_write_to_same_path_replaces_the_entry() -> None:
    project = await _project()
    uploads = _FakeUploads()

    await durability.put_project_file(_WS, _USER, project.id, "a.ts", "v1", uploads=uploads)
    _path, second_id = await durability.put_project_file(
        _WS, _USER, project.id, "a.ts", "v2", uploads=uploads
    )

    overlay = (await codeproject_service.get_project(_WS, _USER, project.id)).overlay
    # ONE entry, pointing at the newest blob — not two rows for one file.
    assert overlay == {"a.ts": second_id}
    assert len(overlay) == 1

    files = await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)
    assert files == {"a.ts": "v2"}


async def test_put_normalizes_a_leading_slash_so_the_key_is_stable() -> None:
    project = await _project()
    uploads = _FakeUploads()

    path, _ = await durability.put_project_file(
        _WS, _USER, project.id, "/src/x.ts", "X", uploads=uploads
    )

    assert path == "src/x.ts"
    assert (await codeproject_service.get_project(_WS, _USER, project.id)).overlay == {
        "src/x.ts": "file-1"
    }


@pytest.mark.parametrize("bad", ["", "   ", "/", "../etc/passwd", "src/../../escape.ts"])
async def test_put_rejects_paths_that_escape_the_project(bad) -> None:  # noqa: ANN001
    project = await _project()
    uploads = _FakeUploads()

    with pytest.raises(ValidationError):
        await durability.put_project_file(_WS, _USER, project.id, bad, "x", uploads=uploads)
    assert uploads.upload_calls == []


# ---------------------------------------------------------------------------
# Round-trip — write, then read the whole overlay back byte-identical.
# ---------------------------------------------------------------------------


async def test_round_trip_returns_exactly_the_written_files() -> None:
    project = await _project()
    uploads = _FakeUploads()

    await durability.put_project_file(
        _WS, _USER, project.id, "index.html", "<h1>hi</h1>", uploads=uploads
    )
    await durability.put_project_file(
        _WS,
        _USER,
        project.id,
        "src/lib/x.ts",
        "export const x = 1;\n// naïve — üñí\n",
        uploads=uploads,
    )

    files = await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)

    assert files == {
        "index.html": "<h1>hi</h1>",
        "src/lib/x.ts": "export const x = 1;\n// naïve — üñí\n",
    }
    # Byte-identical, not merely equal-looking: what went in came back out.
    assert files["src/lib/x.ts"].encode("utf-8") == uploads.upload_calls[1]["data"]


async def test_read_back_is_empty_for_an_untouched_project() -> None:
    # A fresh project stores NOTHING — the scaffold baseline is re-materialized
    # client-side from the starter id, so the overlay is the delta only.
    project = await _project()
    uploads = _FakeUploads()

    assert await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads) == {}
    assert uploads.stream_calls == []


async def test_read_back_skips_an_unreadable_blob_instead_of_failing() -> None:
    project = await _project()
    uploads = _FakeUploads()
    await durability.put_project_file(_WS, _USER, project.id, "keep.ts", "K", uploads=uploads)
    # A pointer whose blob was reaped from storage: one bad entry must not sink
    # the whole restore.
    await codeproject_service.set_project_overlay_entry(
        _WS, _USER, project.id, "gone.ts", "file-missing"
    )

    files = await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)
    assert files == {"keep.ts": "K"}


# ---------------------------------------------------------------------------
# Delete — a dropped file does not come back on the next restore.
# ---------------------------------------------------------------------------


async def test_delete_drops_the_file_from_the_read_back() -> None:
    project = await _project()
    uploads = _FakeUploads()
    await durability.put_project_file(_WS, _USER, project.id, "a.ts", "A", uploads=uploads)
    await durability.put_project_file(_WS, _USER, project.id, "dir/b.ts", "B", uploads=uploads)

    dropped = await durability.drop_project_file(_WS, _USER, project.id, "a.ts")

    assert dropped == "a.ts"
    files = await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)
    assert files == {"dir/b.ts": "B"}


async def test_delete_normalizes_the_same_way_the_write_did() -> None:
    # The write stored ``src/a.ts``; a delete spelled ``/src/a.ts`` must still
    # match, or the file silently reappears on the next reopen.
    project = await _project()
    uploads = _FakeUploads()
    await durability.put_project_file(_WS, _USER, project.id, "src/a.ts", "A", uploads=uploads)

    await durability.drop_project_file(_WS, _USER, project.id, "/src/a.ts")

    assert await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads) == {}


async def test_delete_of_a_directory_drops_every_child() -> None:
    project = await _project()
    uploads = _FakeUploads()
    await durability.put_project_file(_WS, _USER, project.id, "src/a.ts", "A", uploads=uploads)
    await durability.put_project_file(_WS, _USER, project.id, "src/deep/b.ts", "B", uploads=uploads)
    await durability.put_project_file(_WS, _USER, project.id, "keep.ts", "K", uploads=uploads)

    await durability.drop_project_file(_WS, _USER, project.id, "src")

    assert await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads) == {
        "keep.ts": "K"
    }


# ---------------------------------------------------------------------------
# Tenancy — every verb is owner-scoped; a foreign caller sees NotFound.
# ---------------------------------------------------------------------------


async def test_put_denies_a_foreign_workspace() -> None:
    project = await _project()
    uploads = _FakeUploads()

    with pytest.raises(NotFound):
        await durability.put_project_file(
            "w2", _USER, project.id, "a.ts", "MINE NOW", uploads=uploads
        )
    # Nothing leaked into the owner's project.
    assert (await codeproject_service.get_project(_WS, _USER, project.id)).overlay == {}


async def test_put_denies_another_user_in_the_same_workspace() -> None:
    project = await _project()
    uploads = _FakeUploads()

    with pytest.raises(NotFound):
        await durability.put_project_file(
            _WS, "u2", project.id, "a.ts", "MINE NOW", uploads=uploads
        )
    assert (await codeproject_service.get_project(_WS, _USER, project.id)).overlay == {}


async def test_read_back_denies_a_foreign_caller() -> None:
    project = await _project()
    uploads = _FakeUploads()
    await durability.put_project_file(_WS, _USER, project.id, "secret.ts", "S", uploads=uploads)

    with pytest.raises(NotFound):
        await durability.read_project_overlay("w2", _USER, project.id, uploads=uploads)
    with pytest.raises(NotFound):
        await durability.read_project_overlay(_WS, "u2", project.id, uploads=uploads)
    # No blob was even reached — the tenancy gate is BEFORE storage.
    assert uploads.stream_calls == []


async def test_delete_denies_a_foreign_caller() -> None:
    project = await _project()
    uploads = _FakeUploads()
    await durability.put_project_file(_WS, _USER, project.id, "a.ts", "A", uploads=uploads)

    with pytest.raises(NotFound):
        await durability.drop_project_file("w2", _USER, project.id, "a.ts")
    with pytest.raises(NotFound):
        await durability.drop_project_file(_WS, "u2", project.id, "a.ts")
    # Still there for the real owner.
    assert await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads) == {
        "a.ts": "A"
    }


# ---------------------------------------------------------------------------
# S3 guard — the durable WRITE path still fails closed in cloud without s3.
# ---------------------------------------------------------------------------


async def test_put_raises_in_cloud_without_s3(monkeypatch) -> None:  # noqa: ANN001
    project = await _project()
    uploads = _FakeUploads()

    monkeypatch.setattr(durability, "is_multi_tenant_cloud", lambda: True)
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "local")

    with pytest.raises(CloudError) as exc:
        await durability.put_project_file(_WS, _USER, project.id, "a.ts", "A", uploads=uploads)
    assert exc.value.code == "codeproject.durable_store_requires_s3"
    assert exc.value.status_code == 503
    # Fails CLOSED — nothing was uploaded and no pointer was recorded.
    assert uploads.upload_calls == []
    assert (await codeproject_service.get_project(_WS, _USER, project.id)).overlay == {}


async def test_put_ok_in_cloud_with_s3(monkeypatch) -> None:  # noqa: ANN001
    project = await _project()
    uploads = _FakeUploads()

    monkeypatch.setattr(durability, "is_multi_tenant_cloud", lambda: True)
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "s3")

    path, file_id = await durability.put_project_file(
        _WS, _USER, project.id, "a.ts", "A", uploads=uploads
    )
    assert (path, file_id) == ("a.ts", "file-1")


# ---------------------------------------------------------------------------
# Caps — bounded, and LOUD rather than silently partial.
# ---------------------------------------------------------------------------


async def test_write_over_the_per_file_cap_is_rejected(monkeypatch) -> None:  # noqa: ANN001
    project = await _project()
    uploads = _FakeUploads()
    # The PER-FILE knob, split from the aggregate one in S1: the aggregate had to
    # grow to hold a whole source tree, and one runaway file must stay bounded.
    monkeypatch.setenv("POCKETPAW_CODEPROJECT_FILE_MAX_MB", "0.000001")  # ~1 byte

    with pytest.raises(CloudError) as exc:
        await durability.put_project_file(
            _WS, _USER, project.id, "big.ts", "far too many bytes", uploads=uploads
        )
    assert exc.value.code == "codeproject.file_too_large"
    assert exc.value.status_code == 413
    assert uploads.upload_calls == []


async def test_read_back_over_the_file_count_cap_raises(monkeypatch) -> None:  # noqa: ANN001
    project = await _project()
    uploads = _FakeUploads()
    await durability.put_project_file(_WS, _USER, project.id, "a.ts", "A", uploads=uploads)
    await durability.put_project_file(_WS, _USER, project.id, "b.ts", "B", uploads=uploads)

    monkeypatch.setenv("POCKETPAW_CODEPROJECT_OVERLAY_MAX_FILES", "1")

    with pytest.raises(CloudError) as exc:
        await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)
    # Loud, not truncated: a half-restored project looks like data loss.
    assert exc.value.code == "codeproject.overlay_too_many_files"
    assert exc.value.status_code == 413


async def test_read_back_over_the_byte_cap_raises(monkeypatch) -> None:  # noqa: ANN001
    project = await _project()
    uploads = _FakeUploads()
    await durability.put_project_file(_WS, _USER, project.id, "a.ts", "AAAA", uploads=uploads)

    monkeypatch.setenv("POCKETPAW_CODEPROJECT_OVERLAY_MAX_MB", "0.000001")  # ~1 byte

    with pytest.raises(CloudError) as exc:
        await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)
    assert exc.value.code == "codeproject.overlay_too_large"
    assert exc.value.status_code == 413


# ---------------------------------------------------------------------------
# Router wiring — the three routes reach the storage layer and shape the wire.
# ---------------------------------------------------------------------------


async def test_routes_round_trip_a_file(monkeypatch) -> None:  # noqa: ANN001
    project = await _project()
    uploads = _FakeUploads()
    # The routes build their own uploads service; pin it to the fake so no test
    # touches real storage.
    monkeypatch.setattr(durability, "build_uploads", lambda: uploads)

    written = await codeproject_router.put_project_file(
        project.id,
        codeproject_router.PutProjectFileRequest(path="/src/app.ts", content="hello"),
        ctx=_ctx(),
    )
    assert written.ok is True
    assert written.path == "src/app.ts"  # the normalized key the overlay stored
    assert written.fileId == "file-1"

    listing = await codeproject_router.read_project_files(project.id, ctx=_ctx())
    assert listing.files == {"src/app.ts": "hello"}

    resp = await codeproject_router.delete_project_file(project.id, path="src/app.ts", ctx=_ctx())
    assert resp.status_code == 204
    assert (await codeproject_router.read_project_files(project.id, ctx=_ctx())).files == {}


async def test_routes_are_owner_scoped(monkeypatch) -> None:  # noqa: ANN001
    project = await _project()
    uploads = _FakeUploads()
    monkeypatch.setattr(durability, "build_uploads", lambda: uploads)

    foreign = _ctx(workspace="w2", user="u2")
    with pytest.raises(NotFound):
        await codeproject_router.read_project_files(project.id, ctx=foreign)
    with pytest.raises(NotFound):
        await codeproject_router.put_project_file(
            project.id,
            codeproject_router.PutProjectFileRequest(path="a.ts", content="x"),
            ctx=foreign,
        )
