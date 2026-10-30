# test_codeproject_durability.py — unit tests for the PROJECT-KEYED durable store
# (F, feat/code-durable-project-store): S3 snapshot + per-file overlay anchored on
# the durable CodeProject row, round-tripped through EEUploadService independent of
# any runtime and independent of the ephemeral WebSandbox row.
#
# Created 2026-07-24 (feat/code-durable-project-store).
#
# All Daytona AND blob-storage interaction goes through FAKES injected via the DI
# seams (``client=`` + ``uploads=``) — no test touches real Daytona or S3. The
# CodeProject registry runs on real Beanie over mongomock-motor (the ``mongo_db``
# fixture) so ``set_project_snapshot``'s owner-scoped write and ``get_project``'s
# tenant filter are exercised for real. The VM to snapshot/restore is passed as an
# explicit Daytona ``sandbox_id`` string (never resolved from a WebSandbox row) —
# which is exactly how these tests prove "no WebSandbox row involved".
#
# Covers:
#   * round-trip: the per-file store is written by id, then restored byte-for-byte
#     — with NO WebSandbox row created anywhere.
#   * independence: two projects sharing the SAME starter ``repo`` in one
#     workspace hold independent stores (no collision).
#   * overlay drop / move re-key the durable project pointer.
#   * S3 guard: the project-keyed durable WRITE path RAISES a clean CloudError in
#     cloud when the upload adapter isn't s3; does NOT raise on a non-cloud/OSS
#     install.
#
# Updated 2026-07-25 (S1, feat/code-s3-authoritative): every ``snapshot_project``
# case became a ``sync_project_files`` case. The tarball tier is retired (a tar
# cannot express a DELETE, so replaying it resurrected removed files) and the
# per-file store is the whole truth, so what these prove now is that a workspace
# re-image lands one blob per file and that a full snapshot no longer wipes the
# store. The delete/completeness behaviour has its own suite:
# ``test_codeproject_s3_authoritative.py``.
from __future__ import annotations

import io
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.models.web_sandbox import WebSandbox as _WebSandboxDoc
from pocketpaw_ee.cloud.websandbox import durability
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR

from pocketpaw.uploads.errors import NotFound as UploadNotFound
from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "w1"
_USER = "u1"
_SANDBOX = "dtn-1"  # a Daytona id passed straight in — no WebSandbox row behind it


# ---------------------------------------------------------------------------
# Fakes (same shape as the websandbox durability suite).
# ---------------------------------------------------------------------------


@dataclass
class _FakeDaytonaClient:
    tar_bytes: bytes = b"CANNED-TAR-BYTES"
    exec_calls: list[str] = field(default_factory=list)
    download_calls: list[str] = field(default_factory=list)
    upload_calls: list[dict] = field(default_factory=list)

    async def execute_command(self, sandbox_id, command, **kwargs):  # noqa: ANN001
        self.exec_calls.append(command)
        return None

    async def download_file(self, sandbox_id, remote_path):  # noqa: ANN001
        self.download_calls.append(remote_path)
        return self.tar_bytes

    async def upload_bytes(self, sandbox_id, data, remote_path):  # noqa: ANN001
        self.upload_calls.append(
            {"sandbox_id": sandbox_id, "data": data, "remote_path": remote_path}
        )


class _FakeUploads:
    """In-memory stand-in for EEUploadService: records ``upload`` and serves the
    bytes back on ``stream``. A single instance carries the blob so a snapshot
    written here is readable by a later restore in the same test."""

    def __init__(self) -> None:
        self.upload_calls: list[dict] = []
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
            mime="application/gzip",
            size=len(data),
            owner_id=owner_id,
            chat_id=None,
            created=datetime.now(UTC),
        )

    async def stream(self, file_id, requester_id, workspace):  # noqa: ANN001
        if file_id not in self._blobs:
            raise UploadNotFound()
        data = self._blobs[file_id]
        rec = FileRecord(
            id=file_id,
            storage_key=f"key/{file_id}",
            filename="code-project-snapshot.tgz",
            mime="application/gzip",
            size=len(data),
            owner_id=requester_id,
            chat_id=None,
            created=datetime.now(UTC),
        )

        async def _iter():
            yield data

        return rec, _iter()


def _tar(files: dict[str, bytes]) -> bytes:
    """A REAL gzipped tar shaped like ``tar -czf ... -C <workdir> .`` output.

    The sync path reads the workspace THROUGH a tarball (one round trip instead of
    one download per file), so the fake VM has to hand back a real archive rather
    than a canned string. Members are named ``./x`` exactly as tar writes them.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        for name, blob in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            archive.addfile(info, io.BytesIO(blob))
    return buf.getvalue()


async def _project(workspace=_WS, user=_USER, repo="react", provider="starter", name=None):  # noqa: ANN001
    """Create a durable project row (starter by default, so two share one repo)."""
    body = {"repo": repo, "provider": provider}
    if name is not None:
        body["name"] = name
    return await codeproject_service.create_project(workspace, user, body)


# ---------------------------------------------------------------------------
# Round-trip — snapshot + overlay written to a project, restored byte-for-byte,
# with NO WebSandbox row involved.
# ---------------------------------------------------------------------------


async def test_sync_uploads_every_workspace_file_and_records_the_store() -> None:
    project = await _project()
    fake = _FakeDaytonaClient(tar_bytes=_tar({"./a.ts": b"AAA", "./dir/b.ts": b"BBB"}))
    uploads = _FakeUploads()

    overlay = await durability.sync_project_files(
        _WS, _USER, project.id, _SANDBOX, client=fake, uploads=uploads
    )

    # Tarred the workspace dir in the VM as a TRANSPORT, then downloaded it once.
    assert any("tar -czf" in c and WEBSANDBOX_WORKDIR in c for c in fake.exec_calls)
    assert fake.download_calls == ["/tmp/code-project-sync.tgz"]

    # One blob PER FILE — workspace + owner scoped, in the overlay folder. Nothing
    # lands in the retired snapshots folder.
    assert len(uploads.upload_calls) == 2
    assert {u["folder_path"] for u in uploads.upload_calls} == {"/code-project-overlay"}
    assert {u["workspace"] for u in uploads.upload_calls} == {_WS}
    assert {u["owner_id"] for u in uploads.upload_calls} == {_USER}
    assert {u["data"] for u in uploads.upload_calls} == {b"AAA", b"BBB"}

    # The store is the whole map, persisted on the PROJECT row and marked verified.
    assert set(overlay) == {"a.ts", "dir/b.ts"}
    fetched = await codeproject_service.get_project(_WS, _USER, project.id)
    assert fetched.overlay == overlay
    assert fetched.overlay_complete is True
    assert fetched.snapshot_file_id is None

    # No WebSandbox row was ever created — the store is project-keyed.
    assert await _WebSandboxDoc.find_all().to_list() == []


async def test_round_trip_sync_and_overlay_restores_bytes_no_websandbox() -> None:
    project = await _project()
    uploads = _FakeUploads()

    # A disconnect sync, then a fresh session's edits mirror in on top.
    await durability.sync_project_files(
        _WS,
        _USER,
        project.id,
        _SANDBOX,
        client=_FakeDaytonaClient(tar_bytes=_tar({"./from-the-vm.ts": b"VM"})),
        uploads=uploads,
    )
    await durability.mirror_file_to_project(_WS, _USER, project.id, "a.ts", b"AAA", uploads=uploads)
    await durability.mirror_file_to_project(
        _WS, _USER, project.id, "dir/b.ts", b"BBB", uploads=uploads
    )

    # Restore into a FRESH VM (new fake client, brand-new Daytona id).
    fresh = _FakeDaytonaClient()
    await durability.restore_project(
        _WS, _USER, project.id, "dtn-fresh", client=fresh, uploads=uploads
    )

    # No tarball is fetched or untarred — the per-file store is the whole truth.
    assert not any("tar -xzf" in c for c in fresh.exec_calls)

    # Every file is written byte-for-byte into the jail at WORKDIR/relpath.
    written = {u["remote_path"]: u["data"] for u in fresh.upload_calls}
    assert written[f"{WEBSANDBOX_WORKDIR}/from-the-vm.ts"] == b"VM"
    assert written[f"{WEBSANDBOX_WORKDIR}/a.ts"] == b"AAA"
    assert written[f"{WEBSANDBOX_WORKDIR}/dir/b.ts"] == b"BBB"
    assert any(f"mkdir -p {WEBSANDBOX_WORKDIR}/dir" in c for c in fresh.exec_calls)

    # The whole round-trip never touched a WebSandbox row.
    assert await _WebSandboxDoc.find_all().to_list() == []


async def test_mirror_records_the_store_and_a_legacy_snapshot_no_longer_wipes_it() -> None:
    project = await _project()
    uploads = _FakeUploads()

    file_id = await durability.mirror_file_to_project(
        _WS, _USER, project.id, "src/app.ts", b"console.log(1)", uploads=uploads
    )
    up = uploads.upload_calls[0]
    assert up["folder_path"] == "/code-project-overlay"
    assert up["data"] == b"console.log(1)"
    assert file_id == "file-1"
    assert (await codeproject_service.get_project(_WS, _USER, project.id)).overlay == {
        "src/app.ts": "file-1"
    }

    # Recording a legacy tarball pointer must NOT clear the store. It used to (a
    # "full snapshot supersedes the delta"), and that is precisely what threw away
    # the only tier able to record a deletion.
    await codeproject_service.set_project_snapshot(_WS, _USER, project.id, "legacy-tar")
    kept = await codeproject_service.get_project(_WS, _USER, project.id)
    assert kept.overlay == {"src/app.ts": "file-1"}
    assert kept.snapshot_file_id == "legacy-tar"


async def test_restore_overlay_only_no_snapshot() -> None:
    project = await _project()
    fake = _FakeDaytonaClient()
    uploads = _FakeUploads()

    await durability.mirror_file_to_project(
        _WS, _USER, project.id, "only.ts", b"ONLY", uploads=uploads
    )
    await durability.restore_project(_WS, _USER, project.id, _SANDBOX, client=fake, uploads=uploads)

    assert not any("tar -xzf" in c for c in fake.exec_calls)
    written = {u["remote_path"]: u["data"] for u in fake.upload_calls}
    assert written[f"{WEBSANDBOX_WORKDIR}/only.ts"] == b"ONLY"


async def test_restore_with_neither_snapshot_nor_overlay_is_conflict() -> None:
    project = await _project()
    fake = _FakeDaytonaClient()
    uploads = _FakeUploads()

    with pytest.raises(CloudError) as exc:
        await durability.restore_project(
            _WS, _USER, project.id, _SANDBOX, client=fake, uploads=uploads
        )
    assert exc.value.code == "codeproject.no_snapshot"


async def test_drop_project_overlay_removes_entry() -> None:
    project = await _project()
    uploads = _FakeUploads()
    await durability.mirror_file_to_project(
        _WS, _USER, project.id, "dir/a.ts", b"A", uploads=uploads
    )
    await durability.mirror_file_to_project(
        _WS, _USER, project.id, "keep.ts", b"K", uploads=uploads
    )

    await durability.drop_project_overlay(_WS, _USER, project.id, "dir")

    assert (await codeproject_service.get_project(_WS, _USER, project.id)).overlay == {
        "keep.ts": "file-2"
    }


async def test_move_project_overlay_rekeys() -> None:
    project = await _project()
    uploads = _FakeUploads()
    await durability.mirror_file_to_project(_WS, _USER, project.id, "a.ts", b"A", uploads=uploads)

    await durability.move_project_overlay(_WS, _USER, project.id, "a.ts", "b.ts")

    assert (await codeproject_service.get_project(_WS, _USER, project.id)).overlay == {
        "b.ts": "file-1"
    }


# ---------------------------------------------------------------------------
# Independence — two projects, one starter repo, one workspace: no collision.
# ---------------------------------------------------------------------------


async def test_two_projects_same_starter_hold_independent_state() -> None:
    # Same workspace, same user, same starter template — two distinct projects
    # (scaffold rows carry a per-row registry token, so they never dedupe).
    todo = await _project(repo="react", provider="starter", name="todo")
    blog = await _project(repo="react", provider="starter", name="blog")
    assert todo.id != blog.id

    uploads = _FakeUploads()
    await durability.sync_project_files(
        _WS,
        _USER,
        todo.id,
        _SANDBOX,
        client=_FakeDaytonaClient(tar_bytes=_tar({"./synced.ts": b"S"})),
        uploads=uploads,
    )
    await durability.mirror_file_to_project(_WS, _USER, todo.id, "todo.ts", b"T", uploads=uploads)
    await durability.mirror_file_to_project(_WS, _USER, blog.id, "blog.ts", b"B", uploads=uploads)

    todo_view = await codeproject_service.get_project(_WS, _USER, todo.id)
    blog_view = await codeproject_service.get_project(_WS, _USER, blog.id)

    # The sync landed only on todo; each store is the project's own.
    assert todo_view.overlay_complete is True
    assert blog_view.overlay_complete is False
    assert todo_view.overlay == {"synced.ts": "file-1", "todo.ts": "file-2"}
    assert blog_view.overlay == {"blog.ts": "file-3"}


# ---------------------------------------------------------------------------
# Tenancy — a cross-tenant caller is denied before any VM/S3 op.
# ---------------------------------------------------------------------------


async def test_sync_denies_cross_tenant_caller() -> None:
    project = await _project(workspace=_WS, user=_USER)
    fake = _FakeDaytonaClient()
    uploads = _FakeUploads()

    from pocketpaw_ee.cloud._core.errors import NotFound

    with pytest.raises(NotFound):
        await durability.sync_project_files(
            "w2", _USER, project.id, _SANDBOX, client=fake, uploads=uploads
        )
    assert fake.exec_calls == []
    assert uploads.upload_calls == []


# ---------------------------------------------------------------------------
# S3 guard — the durable WRITE path fails closed in cloud without an s3 adapter,
# but never raises off cloud (OSS / dedicated).
# ---------------------------------------------------------------------------


async def test_sync_write_raises_in_cloud_without_s3(monkeypatch) -> None:
    project = await _project()
    fake = _FakeDaytonaClient()
    uploads = _FakeUploads()

    # Force the multi-tenant-cloud signal on, and leave the adapter non-s3.
    monkeypatch.setattr(durability, "is_multi_tenant_cloud", lambda: True)
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "local")

    with pytest.raises(CloudError) as exc:
        await durability.sync_project_files(
            _WS, _USER, project.id, _SANDBOX, client=fake, uploads=uploads
        )
    assert exc.value.code == "codeproject.durable_store_requires_s3"
    assert exc.value.status_code == 503
    # Fails CLOSED before any VM or S3 op.
    assert fake.exec_calls == []
    assert uploads.upload_calls == []


async def test_mirror_write_raises_in_cloud_without_s3(monkeypatch) -> None:
    project = await _project()
    uploads = _FakeUploads()

    monkeypatch.setattr(durability, "is_multi_tenant_cloud", lambda: True)
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "local")

    with pytest.raises(CloudError) as exc:
        await durability.mirror_file_to_project(
            _WS, _USER, project.id, "a.ts", b"A", uploads=uploads
        )
    assert exc.value.code == "codeproject.durable_store_requires_s3"
    assert uploads.upload_calls == []


async def test_sync_write_ok_in_cloud_with_s3(monkeypatch) -> None:
    project = await _project()
    fake = _FakeDaytonaClient(tar_bytes=_tar({"./ok.ts": b"OK"}))
    uploads = _FakeUploads()

    monkeypatch.setattr(durability, "is_multi_tenant_cloud", lambda: True)
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "s3")

    overlay = await durability.sync_project_files(
        _WS, _USER, project.id, _SANDBOX, client=fake, uploads=uploads
    )
    assert overlay == {"ok.ts": "file-1"}


async def test_write_does_not_raise_off_cloud(monkeypatch) -> None:
    # OSS / dedicated install: the cloud DB was never initialized, so the guard is
    # a no-op even with a local adapter — local-disk uploads are correct there.
    project = await _project()
    uploads = _FakeUploads()

    monkeypatch.setattr(durability, "is_multi_tenant_cloud", lambda: False)
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "local")

    file_id = await durability.mirror_file_to_project(
        _WS, _USER, project.id, "a.ts", b"A", uploads=uploads
    )
    assert file_id == "file-1"
