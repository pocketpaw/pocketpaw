# test_codeproject_cross_runtime.py — the CROSS-RUNTIME restore matrix (B4,
# feat/code-cross-runtime-restore): whatever a project saved in one runtime must be
# retrievable from the other.
#
# Created 2026-07-25 (feat/code-cross-runtime-restore).
#
# The bug these guard: a project worked on in DAYTONA lands its state in the
# snapshot tar, and ``set_project_snapshot`` clears the overlay (correctly — the tar
# supersedes it). The in-tab read-back looked only at the overlay, so reopening that
# same project in a browser tab returned ``{}`` and the client re-materialized the
# bare starter scaffold. The user's work looked deleted while it sat safe in S3
# inside a tarball no browser can read.
#
# Fakes only: blob storage through the ``uploads=`` DI seam, Daytona through
# ``client=`` — no test touches real S3 or a real VM. The CodeProject registry runs
# on real Beanie over mongomock-motor (the ``mongo_db`` fixture) so the owner-scoped
# snapshot/overlay writes are exercised for real. Snapshot blobs are REAL gzipped
# tars built in memory with ``tarfile``, so the expander is tested against the exact
# archive shape ``tar -czf ... -C <workdir> .`` produces (members named ``./x``).
#
# Covers:
#   * Daytona -> in-tab: a snapshot with an EMPTY overlay reads back the tar's files.
#   * merge order: a path in BOTH tiers reads back the OVERLAY's content.
#   * in-tab -> Daytona: an overlay-only project (snapshot_file_id is None) restores
#     into a fresh VM without needing a snapshot.
#   * filtering: node_modules / .git / build output and binary entries are excluded;
#     ordinary nested source (incl. non-ASCII text) survives byte-identical.
#   * tar safety: absolute, ``..``-traversing, and symlink members are skipped.
#   * caps: a filtered snapshot still over the cap fails LOUD, never partial.
#
# Updated 2026-07-25 (S1, feat/code-s3-authoritative): ``snapshot_project`` is gone —
# the tarball tier was retired because a tar cannot express a DELETE — so the
# "a Daytona session ended" setup now stages the LEGACY row shape directly (tar in
# blob storage + ``snapshot_file_id`` pointing at it). That is deliberately still the
# scenario under test: the browser read-back must keep serving such a project until a
# VM open migrates it to per-file entries. The read path itself is unchanged.
from __future__ import annotations

import io
import logging
import tarfile
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.codeproject import router as codeproject_router
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.websandbox import durability
from pocketpaw_ee.cloud.websandbox import ws as terminal_ws
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR

from pocketpaw.uploads.errors import NotFound as UploadNotFound
from pocketpaw.uploads.file_store import FileRecord

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "w1"
_USER = "u1"
_SANDBOX = "dtn-1"


# ---------------------------------------------------------------------------
# Fakes + tar builder.
# ---------------------------------------------------------------------------


class _FakeDaytonaClient:
    """Records VM ops and serves a canned tarball on download (the snapshot)."""

    def __init__(self, tar_bytes: bytes = b"") -> None:
        self.tar_bytes = tar_bytes
        self.exec_calls: list[str] = []
        self.upload_calls: list[dict] = []

    async def execute_command(self, sandbox_id, command, **kwargs):  # noqa: ANN001
        self.exec_calls.append(command)
        return None

    async def download_file(self, sandbox_id, remote_path):  # noqa: ANN001
        return self.tar_bytes

    async def upload_bytes(self, sandbox_id, data, remote_path):  # noqa: ANN001
        self.upload_calls.append({"data": data, "remote_path": remote_path})


class _FakeUploads:
    """In-memory EEUploadService stand-in: one instance carries the blobs."""

    def __init__(self) -> None:
        self.upload_calls: list[dict] = []
        self._blobs: dict[str, bytes] = {}
        self._counter = 0

    async def upload(self, file, owner_id, chat_id, workspace, folder_path="/", pocket_id=None):  # noqa: ANN001
        data = await file.read()
        self._counter += 1
        file_id = f"file-{self._counter}"
        self.upload_calls.append({"folder_path": folder_path, "data": data})
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
        if file_id not in self._blobs:
            raise UploadNotFound()
        data = self._blobs[file_id]
        rec = FileRecord(
            id=file_id,
            storage_key=f"key/{file_id}",
            filename="blob",
            mime="application/octet-stream",
            size=len(data),
            owner_id=requester_id,
            chat_id=None,
            created=datetime.now(UTC),
        )

        async def _iter():
            yield data

        return rec, _iter()


def _tar(files: dict[str, bytes], *, symlinks: dict[str, str] | None = None) -> bytes:
    """A REAL gzipped tar shaped like ``tar -czf ... -C <workdir> .`` output.

    Arcnames are passed through verbatim so a test can spell a member exactly as the
    expander will see it (``./src/app.ts``, ``/etc/passwd``, ``../escape.ts``).
    Directory entries are emitted for realism — they carry no content and must not
    show up in the payload.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as archive:
        dirs: set[str] = set()
        for name in files:
            parent = name.rsplit("/", 1)[0]
            if parent and parent not in dirs:
                dirs.add(parent)
                info = tarfile.TarInfo(parent)
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
        for name, blob in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(blob)
            archive.addfile(info, io.BytesIO(blob))
        for name, target in (symlinks or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return buf.getvalue()


async def _project(workspace=_WS, user=_USER):  # noqa: ANN001
    return await codeproject_service.create_project(
        workspace, user, {"repo": "react", "provider": "starter"}
    )


async def _daytona_session(project_id: str, uploads: _FakeUploads, tar_bytes: bytes) -> None:
    """Stage a LEGACY tarball snapshot on a project (pre-S1 durable state).

    S1 retired the tarball tier and deleted ``snapshot_project``, so this stages the
    state directly: land the tar in blob storage and point the project at it. That
    is exactly the row shape a project written by the old Daytona path still has in
    Mongo today, which is what these cross-runtime read-back tests are about — the
    browser must keep serving a legacy project's files until a VM open migrates it.
    """
    file_id = await durability._upload_project_blob(
        uploads,
        tar_bytes,
        workspace_id=_WS,
        user_id=_USER,
        filename="legacy-snapshot.tgz",
        folder="/code-project-snapshots",
        content_type="application/gzip",
    )
    await codeproject_service.set_project_snapshot(_WS, _USER, project_id, file_id)


def _ctx(workspace=_WS, user=_USER):  # noqa: ANN001
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="req-1",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# 1. Daytona -> in-tab. THE regression: snapshot set, overlay cleared, read back.
# ---------------------------------------------------------------------------


async def test_snapshot_only_project_reads_back_the_tars_files() -> None:
    project = await _project()
    uploads = _FakeUploads()

    # A pre-S1 Daytona session: everything the user did is inside the tarball and
    # the overlay is empty (the old ``set_project_snapshot`` cleared it, which is
    # exactly the state that made this read-back return ``{}``).
    await _daytona_session(
        project.id,
        uploads,
        _tar({"./src/app.ts": b"console.log('work')", "./README.md": b"# hi"}),
    )
    fetched = await codeproject_service.get_project(_WS, _USER, project.id)
    assert fetched.snapshot_file_id is not None
    assert fetched.overlay == {}  # the state this read-back used to return as {}

    files = await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)

    # The user's work comes back in the browser instead of a bare scaffold.
    assert files == {"src/app.ts": "console.log('work')", "README.md": "# hi"}


async def test_route_serves_the_snapshot_to_the_in_tab_runtime(monkeypatch) -> None:  # noqa: ANN001
    # Same regression one layer up: the wire payload the tab actually replays.
    project = await _project()
    uploads = _FakeUploads()
    monkeypatch.setattr(durability, "build_uploads", lambda: uploads)
    await _daytona_session(project.id, uploads, _tar({"./index.html": b"<h1>hi</h1>"}))

    listing = await codeproject_router.read_project_files(project.id, ctx=_ctx())

    assert listing.files == {"index.html": "<h1>hi</h1>"}


# ---------------------------------------------------------------------------
# 2. Merge order — the overlay is the freshest tier and wins.
# ---------------------------------------------------------------------------


async def test_overlay_wins_over_the_snapshot_for_the_same_path() -> None:
    project = await _project()
    uploads = _FakeUploads()
    await _daytona_session(
        project.id,
        uploads,
        _tar({"./src/app.ts": b"OLD-FROM-TAR", "./only-in-tar.ts": b"BASE"}),
    )
    # An edit made AFTER the snapshot (either runtime writes this tier).
    await durability.put_project_file(
        _WS, _USER, project.id, "src/app.ts", "NEW-FROM-OVERLAY", uploads=uploads
    )

    files = await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)

    assert files == {"src/app.ts": "NEW-FROM-OVERLAY", "only-in-tar.ts": "BASE"}


async def test_tiers_are_composed_in_the_same_order_the_vm_restore_replays() -> None:
    # The VM restore untars the snapshot then writes the overlay over it. The
    # read-back must agree, or the two runtimes disagree about the same project.
    project = await _project()
    uploads = _FakeUploads()
    await _daytona_session(project.id, uploads, _tar({"./a.ts": b"TAR-A"}))
    await durability.put_project_file(_WS, _USER, project.id, "a.ts", "OVERLAY-A", uploads=uploads)

    vm = _FakeDaytonaClient()
    await durability.restore_project(
        _WS, _USER, project.id, "dtn-fresh", client=vm, uploads=uploads
    )
    read_back = await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)

    # The VM's LAST write to a.ts is the overlay copy...
    vm_writes = [u for u in vm.upload_calls if u["remote_path"] == f"{WEBSANDBOX_WORKDIR}/a.ts"]
    assert vm_writes[-1]["data"] == b"OVERLAY-A"
    # ...and the browser payload agrees.
    assert read_back["a.ts"] == "OVERLAY-A"


# ---------------------------------------------------------------------------
# 3. In-tab -> Daytona. The reverse direction, proven rather than assumed.
# ---------------------------------------------------------------------------


async def test_overlay_only_project_restores_into_a_fresh_vm() -> None:
    project = await _project()
    uploads = _FakeUploads()
    # Written from the browser: the in-tab runtime never writes a snapshot.
    await durability.put_project_file(
        _WS, _USER, project.id, "src/app.ts", "from the tab", uploads=uploads
    )
    await durability.put_project_file(_WS, _USER, project.id, "top.ts", "T", uploads=uploads)
    assert (await codeproject_service.get_project(_WS, _USER, project.id)).snapshot_file_id is None

    vm = _FakeDaytonaClient()
    await durability.restore_project(
        _WS, _USER, project.id, "dtn-fresh", client=vm, uploads=uploads
    )

    # No snapshot needed — the overlay replays on its own, byte-for-byte.
    assert not any("tar -xzf" in c for c in vm.exec_calls)
    written = {u["remote_path"]: u["data"] for u in vm.upload_calls}
    assert written[f"{WEBSANDBOX_WORKDIR}/src/app.ts"] == b"from the tab"
    assert written[f"{WEBSANDBOX_WORKDIR}/top.ts"] == b"T"
    assert any(f"mkdir -p {WEBSANDBOX_WORKDIR}/src" in c for c in vm.exec_calls)


async def test_a_tab_written_file_survives_a_round_trip_through_the_vm() -> None:
    # The full loop: tab writes -> VM restores + snapshots -> tab reads it back.
    project = await _project()
    uploads = _FakeUploads()
    await durability.put_project_file(_WS, _USER, project.id, "note.md", "# mine", uploads=uploads)

    vm = _FakeDaytonaClient()
    await durability.restore_project(
        _WS, _USER, project.id, "dtn-fresh", client=vm, uploads=uploads
    )
    # The VM session ends and tars what it restored.
    await _daytona_session(project.id, uploads, _tar({"./note.md": b"# mine"}))

    files = await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)
    assert files == {"note.md": "# mine"}


# ---------------------------------------------------------------------------
# 4. Filtering — regenerable trees and binary blobs never reach the browser.
# ---------------------------------------------------------------------------


async def test_regenerable_trees_and_binaries_are_excluded_but_source_survives() -> None:
    project = await _project()
    uploads = _FakeUploads()
    source = "export const x = 1;\n// naïve — üñí\n"
    await _daytona_session(
        project.id,
        uploads,
        _tar(
            {
                "./node_modules/left-pad/index.js": b"module.exports = 1",
                "./src/node_modules/nested/pkg.js": b"deep dep",
                "./.git/objects/ab/cdef": b"\x00binary-git-object",
                "./dist/bundle.js": b"built",
                "./build/out.js": b"built",
                "./.next/server/page.js": b"built",
                "./.svelte-kit/generated/root.js": b"built",
                "./.turbo/log.txt": b"cached",
                "./.cache/x": b"cached",
                "./coverage/lcov.info": b"report",
                "./assets/logo.png": b"\x89PNG\r\n\x1a\n\xff\xfe",  # not UTF-8
                "./src/deep/nested/mod.ts": source.encode("utf-8"),
                "./package.json": b'{"name":"app"}',
            }
        ),
    )

    files = await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)

    assert set(files) == {"src/deep/nested/mod.ts", "package.json"}
    # Byte-identical, non-ASCII included.
    assert files["src/deep/nested/mod.ts"] == source
    assert files["src/deep/nested/mod.ts"].encode("utf-8") == source.encode("utf-8")


async def test_a_file_named_like_an_excluded_tree_is_kept() -> None:
    # The filter matches whole path SEGMENTS — ``build.ts`` is source, ``build/`` is
    # output. Dropping a user's file because of a prefix would be data loss.
    project = await _project()
    uploads = _FakeUploads()
    await _daytona_session(
        project.id,
        uploads,
        _tar({"./build.ts": b"SRC", "./src/dist-utils.ts": b"SRC2", "./build/x.js": b"OUT"}),
    )

    files = await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)

    assert files == {"build.ts": "SRC", "src/dist-utils.ts": "SRC2"}


# ---------------------------------------------------------------------------
# 5. Tar safety — the archive is untrusted input.
# ---------------------------------------------------------------------------


async def test_unsafe_tar_members_are_skipped_not_written() -> None:
    project = await _project()
    uploads = _FakeUploads()
    await _daytona_session(
        project.id,
        uploads,
        _tar(
            {
                "/etc/passwd": b"root:x:0:0",  # absolute
                "../../escape.ts": b"ESCAPED",  # traversal
                "./src/../../also-escape.ts": b"ESCAPED",  # traversal mid-path
                "./ok.ts": b"OK",
            },
            symlinks={"./link-out.ts": "/etc/passwd", "./inner-link.ts": "ok.ts"},
        ),
    )

    files = await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)

    # Only the one legitimate member survives — no absolute path, no traversal, and
    # no link entry (a symlink is a smuggling vector with no meaning in a tab FS).
    assert files == {"ok.ts": "OK"}
    assert not any(p.startswith("/") or ".." in p for p in files)


async def test_directory_members_do_not_become_files() -> None:
    project = await _project()
    uploads = _FakeUploads()
    await _daytona_session(project.id, uploads, _tar({"./src/a.ts": b"A"}))

    files = await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)

    assert files == {"src/a.ts": "A"}
    assert "src" not in files


# ---------------------------------------------------------------------------
# Caps + failure modes — loud, never silently partial.
# ---------------------------------------------------------------------------


async def test_a_filtered_snapshot_over_the_byte_cap_fails_loud(monkeypatch) -> None:  # noqa: ANN001
    # Policy: what survives filtering IS user work, so a truncated payload would
    # look like deleted files. 413 instead — the project still opens in the VM
    # runtime, whose restore has no such cap.
    project = await _project()
    uploads = _FakeUploads()
    await _daytona_session(project.id, uploads, _tar({"./big.ts": b"x" * 4096}))
    monkeypatch.setenv("POCKETPAW_CODEPROJECT_OVERLAY_MAX_MB", "0.001")  # ~1 KB

    with pytest.raises(CloudError) as exc:
        await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)
    assert exc.value.code == "codeproject.overlay_too_large"
    assert exc.value.status_code == 413


async def test_excluded_bytes_do_not_count_against_the_cap(monkeypatch) -> None:  # noqa: ANN001
    # The complement: a huge node_modules must NOT push a small source tree over
    # the cap, or every real project would 413.
    project = await _project()
    uploads = _FakeUploads()
    await _daytona_session(
        project.id,
        uploads,
        _tar({"./node_modules/huge/blob.js": b"y" * 200_000, "./src/a.ts": b"A"}),
    )
    monkeypatch.setenv("POCKETPAW_CODEPROJECT_OVERLAY_MAX_MB", "0.001")  # ~1 KB

    assert await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads) == {
        "src/a.ts": "A"
    }


async def test_a_corrupt_snapshot_raises_instead_of_returning_a_partial_tree() -> None:
    project = await _project()
    uploads = _FakeUploads()
    await _daytona_session(project.id, uploads, b"NOT-A-TARBALL")
    await durability.put_project_file(_WS, _USER, project.id, "a.ts", "A", uploads=uploads)

    with pytest.raises(CloudError) as exc:
        await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)
    # Returning just the overlay here would silently drop the whole baseline.
    assert exc.value.code == "codeproject.snapshot_unreadable"
    assert exc.value.status_code == 502


async def test_a_truncated_snapshot_raises_too() -> None:
    # Truncation fails in the DECOMPRESSOR mid-iteration, not at open — a different
    # exception family, same "the baseline is gone" outcome, same loud answer.
    project = await _project()
    uploads = _FakeUploads()
    whole = _tar({f"./f{i}.ts": b"x" * 500 for i in range(50)})
    await _daytona_session(project.id, uploads, whole[: len(whole) // 2])

    with pytest.raises(CloudError) as exc:
        await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads)
    assert exc.value.code == "codeproject.snapshot_unreadable"


async def test_a_project_with_neither_tier_still_reads_back_empty() -> None:
    project = await _project()
    uploads = _FakeUploads()

    assert await durability.read_project_overlay(_WS, _USER, project.id, uploads=uploads) == {}


async def test_a_swallowed_durability_failure_is_logged_at_warning(monkeypatch, caplog) -> None:  # noqa: ANN001
    """A misconfigured cloud persists nothing — that must be visible, not debug-only.

    The exact production shape: ``POCKETPAW_UPLOAD_ADAPTER != s3`` in cloud makes the
    project store fail closed on EVERY save. FileRpc swallows the hook failure (right
    — the VM write already landed), so before this fix the only trace was a debug line.
    """
    project = await _project()
    uploads = _FakeUploads()
    monkeypatch.setattr(durability, "is_multi_tenant_cloud", lambda: True)
    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "local")

    on_write, _on_delete, _on_move = terminal_ws.build_durability_hooks(
        _WS, _USER, "row-1", project.id, uploads
    )
    with caplog.at_level(logging.WARNING, logger="pocketpaw_ee.cloud.websandbox.ws"):
        await on_write("src/app.ts", b"A")  # must NOT raise into the file op

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a failed durable write was invisible"
    message = warnings[-1].getMessage()
    assert project.id in message and "src/app.ts" in message
    # Still swallowed, and still genuinely unpersisted (the guard did its job).
    assert (await codeproject_service.get_project(_WS, _USER, project.id)).overlay == {}


async def test_the_snapshot_tier_is_owner_scoped_too() -> None:
    project = await _project()
    uploads = _FakeUploads()
    await _daytona_session(project.id, uploads, _tar({"./secret.ts": b"S"}))

    from pocketpaw_ee.cloud._core.errors import NotFound

    with pytest.raises(NotFound):
        await durability.read_project_overlay("w2", _USER, project.id, uploads=uploads)
    with pytest.raises(NotFound):
        await durability.read_project_overlay(_WS, "u2", project.id, uploads=uploads)
