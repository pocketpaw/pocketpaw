# test_websandbox_durability.py — unit tests for the Web Cursor workspace
# durability slice (WC-S3): S3 snapshot + restore of the VM workspace.
# Created 2026-07-15 (feat/websandbox-s3-durability).
#
# All Daytona AND blob-storage interaction goes through FAKES injected via the DI
# seams (``client=`` + ``uploads=`` on both durability fns) — no test touches
# real Daytona or S3. The registry itself runs on real Beanie over
# mongomock-motor (the ``mongo_db`` fixture) so ``set_snapshot``'s owner-scoped
# write and ``get_sandbox``'s tenant filter are exercised for real.
#
# Covers:
#   * snapshot tars the workspace dir, uploads the tarball bytes to S3 (asserts
#     workspace + owner scoping + folder), and records ``snapshot_file_id`` on
#     the row.
#   * restore reads the row's file_id, fetches the bytes back, uploads them into
#     the VM, and runs the untar command into the workspace dir.
#   * restore with no snapshot → clean CloudError, and no VM/S3 op runs.
#   * both authorize: a cross-tenant caller is denied BEFORE any VM/S3 op.
#   * snapshot over the size cap → clean CloudError before any upload.
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.websandbox import durability
from pocketpaw_ee.cloud.websandbox import service as sandbox_service
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR

from pocketpaw.uploads.errors import NotFound as UploadNotFound
from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


@dataclass
class _FakeDaytonaClient:
    """Records VM ops and returns canned tar bytes. Drop-in for DaytonaClient in
    the durability DI seam. ``tar_bytes`` is what ``download_file`` hands back
    (the snapshot payload)."""

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
            filename="ws-snapshot.tgz",
            mime="application/gzip",
            size=len(data),
            owner_id=requester_id,
            chat_id=None,
            created=datetime.now(UTC),
        )

        async def _iter():
            yield data

        return rec, _iter()


async def _ready_row(workspace="w1", user="u1", sandbox_id="dtn-1"):  # noqa: ANN001
    """Create a registry row already ``ready`` with a bound Daytona id."""
    return await sandbox_service.create_sandbox(
        workspace, user, {"repo": "r", "status": "ready", "sandbox_id": sandbox_id}
    )


# ---------------------------------------------------------------------------
# snapshot.
# ---------------------------------------------------------------------------


async def test_snapshot_tars_uploads_and_records_pointer() -> None:
    row = await _ready_row()
    fake = _FakeDaytonaClient(tar_bytes=b"HELLO-SNAPSHOT")
    uploads = _FakeUploads()

    file_id = await durability.snapshot_workspace("w1", "u1", row.id, client=fake, uploads=uploads)

    # Tarred the workspace dir inside the VM, then downloaded the tarball.
    assert any("tar -czf" in c and WEBSANDBOX_WORKDIR in c for c in fake.exec_calls)
    assert fake.download_calls == ["/tmp/ws-snapshot.tgz"]

    # Uploaded the exact bytes to S3, workspace + owner scoped, in the snapshots
    # folder — the ART-4 EEUploadService contract.
    assert len(uploads.upload_calls) == 1
    up = uploads.upload_calls[0]
    assert up["workspace"] == "w1"
    assert up["owner_id"] == "u1"
    assert up["folder_path"] == "/websandbox-snapshots"
    assert up["data"] == b"HELLO-SNAPSHOT"
    assert up["content_type"] == "application/gzip"

    # The durable pointer is returned AND persisted on the row.
    assert file_id == "file-1"
    fetched = await sandbox_service.get_sandbox("w1", "u1", row.id)
    assert fetched.snapshot_file_id == "file-1"


async def test_snapshot_over_cap_raises_before_upload(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_WEBSANDBOX_SNAPSHOT_MAX_MB", "0.00001")  # ~10 bytes
    row = await _ready_row()
    fake = _FakeDaytonaClient(tar_bytes=b"this payload is bigger than ten bytes")
    uploads = _FakeUploads()

    with pytest.raises(CloudError) as exc:
        await durability.snapshot_workspace("w1", "u1", row.id, client=fake, uploads=uploads)
    assert exc.value.code == "websandbox.snapshot_too_large"

    # No upload was attempted, and the row carries no pointer.
    assert uploads.upload_calls == []
    assert (await sandbox_service.get_sandbox("w1", "u1", row.id)).snapshot_file_id is None


# ---------------------------------------------------------------------------
# write-through overlay (CM-2a′).
# ---------------------------------------------------------------------------


async def test_mirror_file_uploads_and_records_overlay() -> None:
    row = await _ready_row()
    uploads = _FakeUploads()

    file_id = await durability.mirror_file(
        "w1", "u1", row.id, "src/app.ts", b"console.log(1)", uploads=uploads
    )

    # Uploaded the file bytes to the overlay folder, workspace + owner scoped.
    assert len(uploads.upload_calls) == 1
    up = uploads.upload_calls[0]
    assert up["workspace"] == "w1"
    assert up["owner_id"] == "u1"
    assert up["folder_path"] == "/websandbox-overlay"
    assert up["data"] == b"console.log(1)"

    # Recorded relpath -> file_id on the row.
    assert file_id == "file-1"
    assert (await sandbox_service.get_sandbox("w1", "u1", row.id)).overlay == {
        "src/app.ts": "file-1"
    }


async def test_snapshot_clears_the_overlay() -> None:
    row = await _ready_row()
    uploads = _FakeUploads()
    fake = _FakeDaytonaClient(tar_bytes=b"TAR")

    await durability.mirror_file("w1", "u1", row.id, "a.ts", b"AAA", uploads=uploads)
    assert (await sandbox_service.get_sandbox("w1", "u1", row.id)).overlay != {}

    # A full snapshot supersedes the incremental overlay → overlay is wiped.
    await durability.snapshot_workspace("w1", "u1", row.id, client=fake, uploads=uploads)
    assert (await sandbox_service.get_sandbox("w1", "u1", row.id)).overlay == {}


async def test_restore_replays_overlay_over_snapshot() -> None:
    row = await _ready_row()
    fake = _FakeDaytonaClient(tar_bytes=b"SNAPSHOT-TAR")
    uploads = _FakeUploads()

    # Disconnect snapshot (clears overlay), then a fresh session's edits mirror in.
    await durability.snapshot_workspace("w1", "u1", row.id, client=fake, uploads=uploads)
    await durability.mirror_file("w1", "u1", row.id, "a.ts", b"AAA", uploads=uploads)
    await durability.mirror_file("w1", "u1", row.id, "dir/b.ts", b"BBB", uploads=uploads)

    await durability.restore_workspace("w1", "u1", row.id, client=fake, uploads=uploads)

    # Snapshot untarred first, then each overlay file written into the jail at
    # WORKDIR/relpath (with a mkdir -p for the nested dir).
    assert any("tar -xzf" in c for c in fake.exec_calls)
    written = {u["remote_path"]: u["data"] for u in fake.upload_calls}
    assert written[f"{WEBSANDBOX_WORKDIR}/a.ts"] == b"AAA"
    assert written[f"{WEBSANDBOX_WORKDIR}/dir/b.ts"] == b"BBB"
    assert any(f"mkdir -p {WEBSANDBOX_WORKDIR}/dir" in c for c in fake.exec_calls)


async def test_restore_overlay_only_no_snapshot() -> None:
    row = await _ready_row()
    fake = _FakeDaytonaClient()
    uploads = _FakeUploads()

    await durability.mirror_file("w1", "u1", row.id, "only.ts", b"ONLY", uploads=uploads)
    await durability.restore_workspace("w1", "u1", row.id, client=fake, uploads=uploads)

    # No snapshot → no untar, but the overlay file is still replayed.
    assert not any("tar -xzf" in c for c in fake.exec_calls)
    written = {u["remote_path"]: u["data"] for u in fake.upload_calls}
    assert written[f"{WEBSANDBOX_WORKDIR}/only.ts"] == b"ONLY"


async def test_restore_with_neither_snapshot_nor_overlay_is_conflict() -> None:
    row = await _ready_row()
    fake = _FakeDaytonaClient()
    uploads = _FakeUploads()

    with pytest.raises(CloudError) as exc:
        await durability.restore_workspace("w1", "u1", row.id, client=fake, uploads=uploads)
    assert exc.value.code == "websandbox.no_snapshot"


async def test_restore_skips_unsafe_overlay_path() -> None:
    row = await _ready_row()
    fake = _FakeDaytonaClient()
    uploads = _FakeUploads()

    await durability.mirror_file("w1", "u1", row.id, "safe.ts", b"SAFE", uploads=uploads)
    # Hand-inject a traversal path the file.write jail would never produce.
    await sandbox_service.set_overlay_entry("w1", "u1", row.id, "../evil.ts", "file-x")

    await durability.restore_workspace("w1", "u1", row.id, client=fake, uploads=uploads)

    written = {u["remote_path"]: u["data"] for u in fake.upload_calls}
    assert written.get(f"{WEBSANDBOX_WORKDIR}/safe.ts") == b"SAFE"
    # Nothing was written outside the jail.
    assert all("evil.ts" not in p for p in written)


async def test_drop_overlay_removes_entry() -> None:
    row = await _ready_row()
    uploads = _FakeUploads()
    await durability.mirror_file("w1", "u1", row.id, "a.ts", b"A", uploads=uploads)

    await durability.drop_overlay("w1", "u1", row.id, "a.ts")

    # The dropped entry falls back to the snapshot tier on restore (safe).
    assert (await sandbox_service.get_sandbox("w1", "u1", row.id)).overlay == {}


async def test_drop_overlay_prefix_drops_directory_entries() -> None:
    row = await _ready_row()
    uploads = _FakeUploads()
    await durability.mirror_file("w1", "u1", row.id, "dir/a.ts", b"A", uploads=uploads)
    await durability.mirror_file("w1", "u1", row.id, "dir/sub/b.ts", b"B", uploads=uploads)
    await durability.mirror_file("w1", "u1", row.id, "keep.ts", b"K", uploads=uploads)

    # Deleting the directory drops every overlay entry underneath it.
    await durability.drop_overlay("w1", "u1", row.id, "dir")

    assert (await sandbox_service.get_sandbox("w1", "u1", row.id)).overlay == {"keep.ts": "file-3"}


async def test_move_overlay_rekeys_src_to_dst() -> None:
    row = await _ready_row()
    uploads = _FakeUploads()
    await durability.mirror_file("w1", "u1", row.id, "a.ts", b"A", uploads=uploads)

    await durability.move_overlay("w1", "u1", row.id, "a.ts", "b.ts")

    # Same FileRecord id, new key — restore replays the file at its new path.
    assert (await sandbox_service.get_sandbox("w1", "u1", row.id)).overlay == {"b.ts": "file-1"}


async def test_move_overlay_rekeys_directory_children() -> None:
    row = await _ready_row()
    uploads = _FakeUploads()
    await durability.mirror_file("w1", "u1", row.id, "old/a.ts", b"A", uploads=uploads)
    await durability.mirror_file("w1", "u1", row.id, "old/sub/b.ts", b"B", uploads=uploads)

    await durability.move_overlay("w1", "u1", row.id, "old", "new")

    assert (await sandbox_service.get_sandbox("w1", "u1", row.id)).overlay == {
        "new/a.ts": "file-1",
        "new/sub/b.ts": "file-2",
    }


async def test_move_overlay_without_src_entry_is_noop() -> None:
    row = await _ready_row()
    uploads = _FakeUploads()
    await durability.mirror_file("w1", "u1", row.id, "keep.ts", b"K", uploads=uploads)

    # Moving a file that has no overlay entry leaves the overlay untouched.
    await durability.move_overlay("w1", "u1", row.id, "a.ts", "b.ts")

    assert (await sandbox_service.get_sandbox("w1", "u1", row.id)).overlay == {"keep.ts": "file-1"}


async def test_snapshot_not_ready_when_unprovisioned() -> None:
    # A row with no bound Daytona id is a clean 409, not a runtime crash.
    row = await sandbox_service.create_sandbox("w1", "u1", {"repo": "r", "status": "pending"})
    fake = _FakeDaytonaClient()
    uploads = _FakeUploads()
    with pytest.raises(CloudError) as exc:
        await durability.snapshot_workspace("w1", "u1", row.id, client=fake, uploads=uploads)
    assert exc.value.code == "websandbox.not_ready"
    assert fake.exec_calls == []
    assert uploads.upload_calls == []


# ---------------------------------------------------------------------------
# restore.
# ---------------------------------------------------------------------------


async def test_restore_fetches_bytes_and_untars_into_vm() -> None:
    row = await _ready_row()
    uploads = _FakeUploads()
    # Snapshot first so the blob + pointer exist.
    await durability.snapshot_workspace(
        "w1", "u1", row.id, client=_FakeDaytonaClient(tar_bytes=b"SNAP-DATA"), uploads=uploads
    )

    # Restore into a FRESH VM (new fake client).
    fresh = _FakeDaytonaClient()
    await durability.restore_workspace("w1", "u1", row.id, client=fresh, uploads=uploads)

    # The snapshot bytes were pushed into the VM at the staging path...
    assert len(fresh.upload_calls) == 1
    assert fresh.upload_calls[0]["remote_path"] == "/tmp/ws-snapshot.tgz"
    assert fresh.upload_calls[0]["data"] == b"SNAP-DATA"
    # ...and untarred into the workspace dir.
    assert any("tar -xzf" in c and WEBSANDBOX_WORKDIR in c for c in fresh.exec_calls)


async def test_restore_without_snapshot_raises_clean_error() -> None:
    row = await _ready_row()
    fake = _FakeDaytonaClient()
    uploads = _FakeUploads()

    with pytest.raises(CloudError) as exc:
        await durability.restore_workspace("w1", "u1", row.id, client=fake, uploads=uploads)
    assert exc.value.code == "websandbox.no_snapshot"

    # Nothing touched the VM or storage.
    assert fake.exec_calls == []
    assert fake.upload_calls == []


# ---------------------------------------------------------------------------
# authorization — cross-tenant callers are denied before any VM/S3 op.
# ---------------------------------------------------------------------------


async def test_snapshot_denies_cross_tenant_caller() -> None:
    row = await _ready_row(workspace="w1", user="u1")
    fake = _FakeDaytonaClient()
    uploads = _FakeUploads()

    # A caller in a DIFFERENT workspace can't resolve the row at all — denied
    # before any tar/download/upload runs.
    with pytest.raises(CloudError):
        await durability.snapshot_workspace("w2", "u1", row.id, client=fake, uploads=uploads)
    assert fake.exec_calls == []
    assert fake.download_calls == []
    assert uploads.upload_calls == []


async def test_restore_denies_cross_tenant_caller() -> None:
    row = await _ready_row(workspace="w1", user="u1")
    uploads = _FakeUploads()
    # Owner snapshots first so a real pointer exists.
    await durability.snapshot_workspace(
        "w1", "u1", row.id, client=_FakeDaytonaClient(), uploads=uploads
    )

    # Cross-tenant restore is denied before any VM/S3 op.
    fake = _FakeDaytonaClient()
    with pytest.raises(CloudError):
        await durability.restore_workspace("w2", "u1", row.id, client=fake, uploads=uploads)
    assert fake.exec_calls == []
    assert fake.upload_calls == []
