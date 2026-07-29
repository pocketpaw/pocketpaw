# test_codeproject_s3_authoritative.py — the S1 cutover: the per-file store IS the
# project, and the tarball tier is retired as a source of truth.
#
# Created 2026-07-25 (S1, feat/code-s3-authoritative).
#
# The bug this removes: a project's baseline was one gzipped tarball, and a tarball
# is an unmodifiable blob — a DELETE has no representation inside it. Replaying the
# baseline on restore resurrected every file the user had removed, and taking a
# snapshot CLEARED the per-file overlay, which was the only tier that could have
# recorded the absence. A tombstone list would have patched the symptom; making the
# per-file store COMPLETE removes the vector: a delete is the removal of an entry,
# and there is no baseline left to resurrect it.
#
# What is proved here, on real Beanie over mongomock-motor (the ``mongo_db``
# fixture) with a FAKE Daytona VM and FAKE blob storage injected through the
# existing DI seams — no real VM, no real S3:
#   1. THE BUG IS DEAD — a file deleted in the VM is gone after sync + restore, both
#      for a file that passed a write hook and for one that never did (a clone's or
#      a scaffold's file), including when the fresh VM re-materializes it.
#   2. COMPLETENESS — a file that arrived without passing a write hook is in the
#      store after a sync and restores from it.
#   3. LEGACY MIGRATION — a pre-S1 project (tarball pointer + partial overlay)
#      migrates to per-file entries once, loses nothing, clears the pointer, and is
#      idempotent on a second run.
#   4. RESTORE IS STORE-ONLY — a migrated/new project never fetches or untars a
#      tarball again.
#   5. THE DESTRUCTIVE PARTS ARE GATED — an empty enumeration never wipes a store,
#      an entry inside an excluded tree is never dropped by a sync that cannot see
#      it, and the prune never fires for a project whose store isn't verified
#      complete (the in-tab case, whose baseline lives in the browser).
#
# The fake VM models a real filesystem rather than canned bytes, because "the file
# is gone" is a claim about a filesystem: it serves ``find`` from its own tree,
# applies ``rm -f``, absorbs ``upload_bytes`` writes, and packs the tree into a REAL
# gzipped tar on ``download_file``. A canned-blob fake could not tell the difference
# between a deletion that worked and one that silently didn't.
from __future__ import annotations

import shlex

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.websandbox import durability
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR

from tests.cloud.test_codeproject_durability import _FakeUploads, _tar

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "w1"
_USER = "u1"
_VM = "dtn-1"
_FRESH_VM = "dtn-fresh"


# ---------------------------------------------------------------------------
# A fake VM with an actual filesystem.
# ---------------------------------------------------------------------------


class _Resp:
    """The shape ``DaytonaClient.execute_command`` returns (``result`` + ``exit_code``)."""

    def __init__(self, result: str = "", exit_code: int = 0) -> None:
        self.result = result
        self.exit_code = exit_code


class _FakeVm:
    """A Daytona stand-in that keeps a real in-memory workspace tree.

    Implements exactly the four verbs the durability paths use, faithfully enough
    that a deletion is observable end to end:
      * ``tar -czf`` is a no-op; ``download_file`` packs the CURRENT tree (minus the
        regenerable trees the real command excludes) into a real gzipped tar, which
        is the transport the sync reads;
      * ``find`` lists the tree, excluded segments pruned, workspace-prefixed;
      * ``rm -f`` actually removes paths — this is what the prune is judged on;
      * ``upload_bytes`` writes a file, so a restore genuinely materializes.
    """

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files: dict[str, bytes] = dict(files or {})
        self.exec_calls: list[str] = []
        self.download_calls: list[str] = []
        self.upload_paths: list[str] = []

    def _visible(self) -> dict[str, bytes]:
        return {
            path: blob
            for path, blob in self.files.items()
            if not durability._is_excluded_snapshot_path(path)
        }

    async def execute_command(self, sandbox_id, command, **kwargs):  # noqa: ANN001
        self.exec_calls.append(command)
        if command.startswith("find "):
            listing = "\n".join(f"{WEBSANDBOX_WORKDIR}/{p}" for p in sorted(self._visible()))
            return _Resp(result=listing)
        if command.startswith("rm -f "):
            prefix = WEBSANDBOX_WORKDIR.rstrip("/") + "/"
            for token in shlex.split(command)[2:]:
                if token.startswith(prefix):
                    self.files.pop(token[len(prefix) :], None)
        return _Resp()

    async def download_file(self, sandbox_id, remote_path):  # noqa: ANN001
        self.download_calls.append(remote_path)
        return _tar({f"./{p}": blob for p, blob in self._visible().items()})

    async def upload_bytes(self, sandbox_id, data, remote_path):  # noqa: ANN001
        self.upload_paths.append(remote_path)
        prefix = WEBSANDBOX_WORKDIR.rstrip("/") + "/"
        if remote_path.startswith(prefix):
            self.files[remote_path[len(prefix) :]] = data


class _TrackingUploads(_FakeUploads):
    """``_FakeUploads`` plus a record of which blobs were READ back."""

    def __init__(self) -> None:
        super().__init__()
        self.stream_calls: list[str] = []

    async def stream(self, file_id, requester_id, workspace):  # noqa: ANN001
        self.stream_calls.append(file_id)
        return await super().stream(file_id, requester_id, workspace)


async def _project(repo="react", provider="starter"):  # noqa: ANN001
    return await codeproject_service.create_project(
        _WS, _USER, {"repo": repo, "provider": provider}
    )


async def _stage_legacy_snapshot(project_id: str, uploads: _FakeUploads, tar_bytes: bytes) -> str:
    """Put a project into the PRE-S1 row shape: a tarball in blob storage + a pointer.

    Exactly what ``snapshot_project`` used to leave behind, staged directly because
    the writer is gone. This is the state every existing project is in right now.
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
    return file_id


# ---------------------------------------------------------------------------
# 1. THE BUG IS DEAD — a deleted file does not come back.
# ---------------------------------------------------------------------------


async def test_a_file_deleted_in_the_vm_is_gone_after_sync_and_restore() -> None:
    """The headline case, end to end: create -> sync -> delete -> sync -> restore."""
    project = await _project()
    uploads = _FakeUploads()
    vm = _FakeVm({"src/app.ts": b"APP", "src/doomed.ts": b"DOOMED"})

    await durability.sync_project_files(_WS, _USER, project.id, _VM, client=vm, uploads=uploads)
    assert set((await codeproject_service.get_project(_WS, _USER, project.id)).overlay) == {
        "src/app.ts",
        "src/doomed.ts",
    }

    # The user deletes it in the VM (a terminal ``rm``, a delete that never reached
    # a hook — the store must reconcile from the workspace, not from an event).
    vm.files.pop("src/doomed.ts")
    await durability.sync_project_files(_WS, _USER, project.id, _VM, client=vm, uploads=uploads)

    stored = await codeproject_service.get_project(_WS, _USER, project.id)
    assert set(stored.overlay) == {"src/app.ts"}, "the deleted file is still in the store"

    # Restore into a genuinely fresh VM: the deletion has to survive the round trip.
    fresh = _FakeVm()
    await durability.restore_project(
        _WS, _USER, project.id, _FRESH_VM, client=fresh, uploads=uploads
    )
    assert fresh.files == {"src/app.ts": b"APP"}


async def test_a_deleted_clone_file_is_gone_even_though_it_never_passed_a_write_hook() -> None:
    """The case a tombstone-free design has to handle.

    ``src/from-the-clone.ts`` arrived with the ``git clone`` — no write hook ever saw
    it, so under the old design only the tarball knew about it, and only the tarball
    could bring it back. Here the sync captures it and the next sync drops it.
    """
    project = await _project(repo="https://github.com/acme/widgets.git", provider="github")
    uploads = _FakeUploads()
    vm = _FakeVm({"src/from-the-clone.ts": b"CLONED", "package.json": b"{}"})

    await durability.sync_project_files(_WS, _USER, project.id, _VM, client=vm, uploads=uploads)
    vm.files.pop("src/from-the-clone.ts")
    await durability.sync_project_files(_WS, _USER, project.id, _VM, client=vm, uploads=uploads)

    fresh = _FakeVm()
    await durability.restore_project(
        _WS, _USER, project.id, _FRESH_VM, client=fresh, uploads=uploads
    )
    assert fresh.files == {"package.json": b"{}"}


async def test_the_prune_removes_a_deleted_file_the_fresh_vm_re_materialized() -> None:
    """The production shape of the same case: the fresh VM is NOT empty.

    A reopened project cold-provisions, and the VM it gets has just been cloned (or
    scaffolded) — so it holds the baseline copy of the file the user deleted.
    Replaying the store over that tree would leave the file behind, which is the
    resurrection this whole change removes. Restore prunes it.
    """
    project = await _project(repo="https://github.com/acme/widgets.git", provider="github")
    uploads = _FakeUploads()
    vm = _FakeVm({"keep.ts": b"KEEP", "deleted-by-the-user.ts": b"OLD"})
    await durability.sync_project_files(_WS, _USER, project.id, _VM, client=vm, uploads=uploads)
    vm.files.pop("deleted-by-the-user.ts")
    await durability.sync_project_files(_WS, _USER, project.id, _VM, client=vm, uploads=uploads)

    # The fresh VM is a fresh CLONE: both files are back on disk.
    fresh = _FakeVm({"keep.ts": b"KEEP", "deleted-by-the-user.ts": b"OLD"})
    await durability.restore_project(
        _WS, _USER, project.id, _FRESH_VM, client=fresh, uploads=uploads
    )

    assert fresh.files == {"keep.ts": b"KEEP"}
    assert any(c.startswith("rm -f ") for c in fresh.exec_calls)


async def test_the_prune_never_touches_regenerable_trees() -> None:
    """``.git`` and ``node_modules`` are absent from the store BY DESIGN.

    They are excluded from every enumeration, so "not in the store" says nothing
    about them — deleting them would destroy the clone's history and force a
    reinstall on every reopen.
    """
    project = await _project(repo="https://github.com/acme/widgets.git", provider="github")
    uploads = _FakeUploads()
    vm = _FakeVm({"a.ts": b"A"})
    await durability.sync_project_files(_WS, _USER, project.id, _VM, client=vm, uploads=uploads)

    fresh = _FakeVm(
        {
            "a.ts": b"A",
            ".git/HEAD": b"ref: refs/heads/main",
            "node_modules/left-pad/index.js": b"dep",
            "dist/bundle.js": b"built",
        }
    )
    await durability.restore_project(
        _WS, _USER, project.id, _FRESH_VM, client=fresh, uploads=uploads
    )

    assert ".git/HEAD" in fresh.files
    assert "node_modules/left-pad/index.js" in fresh.files
    assert "dist/bundle.js" in fresh.files


# ---------------------------------------------------------------------------
# 2. COMPLETENESS — files that never passed a write hook are in the store.
# ---------------------------------------------------------------------------


async def test_scaffold_output_that_never_passed_a_hook_is_captured_and_restores() -> None:
    project = await _project()
    uploads = _FakeUploads()
    # A materialized starter: none of this went through ``file.write``.
    vm = _FakeVm(
        {
            "package.json": b'{"name":"app"}',
            "src/main.tsx": b"render()",
            "public/favicon.ico": b"\x00\x01binary",  # binary survives: bytes, not text
        }
    )

    overlay = await durability.sync_project_files(
        _WS, _USER, project.id, _VM, client=vm, uploads=uploads
    )
    assert set(overlay) == {"package.json", "src/main.tsx", "public/favicon.ico"}

    fresh = _FakeVm()
    await durability.restore_project(
        _WS, _USER, project.id, _FRESH_VM, client=fresh, uploads=uploads
    )
    assert fresh.files == vm.files


async def test_regenerable_trees_never_enter_the_store() -> None:
    """The complement of completeness: ``node_modules`` is not user work.

    It is excluded at the tar level AND re-checked after expansion, so it never
    costs a blob, never counts against the caps, and never has to be restored.
    """
    project = await _project()
    uploads = _FakeUploads()
    vm = _FakeVm(
        {
            "src/a.ts": b"A",
            "node_modules/left-pad/index.js": b"dep",
            ".git/objects/ab/cdef": b"\x00obj",
            "dist/bundle.js": b"built",
            "build.ts": b"NOT-A-BUILD-DIR",  # segment match, not prefix match
        }
    )

    overlay = await durability.sync_project_files(
        _WS, _USER, project.id, _VM, client=vm, uploads=uploads
    )
    assert set(overlay) == {"src/a.ts", "build.ts"}


# ---------------------------------------------------------------------------
# 3. LEGACY MIGRATION — once, lossless, pointer cleared, idempotent.
# ---------------------------------------------------------------------------


async def test_a_legacy_tarball_migrates_to_per_file_entries_and_loses_nothing() -> None:
    project = await _project()
    uploads = _TrackingUploads()

    # A pre-S1 project: most of the work inside the tar, plus the handful of edits a
    # write hook mirrored after the snapshot was taken.
    await _stage_legacy_snapshot(
        project.id,
        uploads,
        _tar(
            {
                "./src/app.ts": b"OLD-FROM-TAR",
                "./only-in-tar.ts": b"TAR-ONLY",
                "./node_modules/dep/index.js": b"dep",  # filtered, not migrated
            }
        ),
    )
    await durability.mirror_file_to_project(
        _WS, _USER, project.id, "src/app.ts", b"NEW-FROM-OVERLAY", uploads=uploads
    )
    await durability.mirror_file_to_project(
        _WS, _USER, project.id, "only-in-overlay.ts", b"OVERLAY-ONLY", uploads=uploads
    )

    fresh = _FakeVm()
    await durability.restore_project(
        _WS, _USER, project.id, _FRESH_VM, client=fresh, uploads=uploads
    )

    # Nothing is lost, and the fresher tier wins where the two overlap.
    assert fresh.files == {
        "src/app.ts": b"NEW-FROM-OVERLAY",
        "only-in-tar.ts": b"TAR-ONLY",
        "only-in-overlay.ts": b"OVERLAY-ONLY",
    }

    migrated = await codeproject_service.get_project(_WS, _USER, project.id)
    assert set(migrated.overlay) == {"src/app.ts", "only-in-tar.ts", "only-in-overlay.ts"}
    assert migrated.snapshot_file_id is None, "the legacy pointer would migrate again"
    assert migrated.overlay_complete is True


async def test_the_migration_is_idempotent_on_a_second_restore() -> None:
    project = await _project()
    uploads = _TrackingUploads()
    legacy_id = await _stage_legacy_snapshot(project.id, uploads, _tar({"./a.ts": b"A"}))

    await durability.restore_project(
        _WS, _USER, project.id, _FRESH_VM, client=_FakeVm(), uploads=uploads
    )
    first = await codeproject_service.get_project(_WS, _USER, project.id)
    uploads_after_first = len(uploads.upload_calls)

    await durability.restore_project(
        _WS, _USER, project.id, "dtn-third", client=_FakeVm(), uploads=uploads
    )
    second = await codeproject_service.get_project(_WS, _USER, project.id)

    assert second.overlay == first.overlay
    assert second.snapshot_file_id is None
    # No second expansion: the tar was neither re-read nor re-uploaded.
    assert len(uploads.upload_calls) == uploads_after_first
    assert uploads.stream_calls.count(legacy_id) == 1


async def test_a_failed_migration_keeps_the_pointer_and_falls_back_to_the_tar() -> None:
    """Non-destructive by construction: a migration that can't finish must not clear.

    Clearing the pointer on a partial run would strand whatever hadn't been carried
    across. Keeping it means the next open retries, and the untar fallback still
    materializes the session so the user never sees a project with holes in it.
    """
    project = await _project()
    uploads = _FakeUploads()
    await _stage_legacy_snapshot(project.id, uploads, _tar({"./a.ts": b"A"}))

    async def _explode(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("blob storage is down")

    original = durability._upload_project_blob
    durability._upload_project_blob = _explode
    try:
        vm = _FakeVm()
        await durability.restore_project(
            _WS, _USER, project.id, _FRESH_VM, client=vm, uploads=uploads
        )
    finally:
        durability._upload_project_blob = original

    kept = await codeproject_service.get_project(_WS, _USER, project.id)
    assert kept.snapshot_file_id is not None, "a partial migration threw the tar away"
    assert kept.overlay_complete is False, "an incomplete store must not license a prune"
    # The session was still materialized — via the legacy untar fallback.
    assert any("tar -xzf" in c for c in vm.exec_calls)


# ---------------------------------------------------------------------------
# 4. RESTORE IS STORE-ONLY for a migrated / new project.
# ---------------------------------------------------------------------------


async def test_restore_never_fetches_a_tarball_for_a_synced_project() -> None:
    project = await _project()
    uploads = _TrackingUploads()
    vm = _FakeVm({"a.ts": b"A", "dir/b.ts": b"B"})
    await durability.sync_project_files(_WS, _USER, project.id, _VM, client=vm, uploads=uploads)

    fresh = _FakeVm()
    await durability.restore_project(
        _WS, _USER, project.id, _FRESH_VM, client=fresh, uploads=uploads
    )

    assert not any("tar -xzf" in c for c in fresh.exec_calls)
    assert "/tmp/ws-snapshot.tgz" not in fresh.upload_paths, "a tarball was staged in the VM"
    assert fresh.files == {"a.ts": b"A", "dir/b.ts": b"B"}
    # Every blob read was an overlay entry — the store, nothing else.
    stored = await codeproject_service.get_project(_WS, _USER, project.id)
    assert set(uploads.stream_calls) == set(stored.overlay.values())


async def test_restore_after_migration_reads_only_the_store() -> None:
    project = await _project()
    uploads = _TrackingUploads()
    legacy_id = await _stage_legacy_snapshot(project.id, uploads, _tar({"./a.ts": b"A"}))
    await durability.restore_project(
        _WS, _USER, project.id, _FRESH_VM, client=_FakeVm(), uploads=uploads
    )

    uploads.stream_calls.clear()
    fresh = _FakeVm()
    await durability.restore_project(
        _WS, _USER, project.id, "dtn-third", client=fresh, uploads=uploads
    )

    assert legacy_id not in uploads.stream_calls
    assert not any("tar -xzf" in c for c in fresh.exec_calls)
    assert fresh.files == {"a.ts": b"A"}


# ---------------------------------------------------------------------------
# 5. THE DESTRUCTIVE PARTS ARE GATED.
# ---------------------------------------------------------------------------


async def test_an_empty_enumeration_never_wipes_the_store() -> None:
    """A transient VM failure must not read as "the user deleted everything"."""
    project = await _project()
    uploads = _FakeUploads()
    vm = _FakeVm({"a.ts": b"A"})
    await durability.sync_project_files(_WS, _USER, project.id, _VM, client=vm, uploads=uploads)
    before = (await codeproject_service.get_project(_WS, _USER, project.id)).overlay

    kept = await durability.sync_project_files(
        _WS, _USER, project.id, _VM, client=_FakeVm(), uploads=uploads
    )

    assert kept == before
    assert (await codeproject_service.get_project(_WS, _USER, project.id)).overlay == before


async def test_sync_keeps_entries_it_could_not_have_seen() -> None:
    """An overlay entry inside an excluded tree is invisible to the enumeration.

    "The sync didn't see it" is not the same claim as "the user deleted it", so such
    an entry is carried across rather than silently dropped.
    """
    project = await _project()
    uploads = _FakeUploads()
    # Planted through the overlay BOOKKEEPING call rather than through a write:
    # since fix/codeproject-never-store-generated-trees the write path refuses an
    # excluded path outright, so a write can no longer create one of these. An entry
    # like this can still be PRESENT — a store written before that guard has them —
    # and surviving the prune is exactly what this test is about.
    await codeproject_service.set_project_overlay_entry(
        _WS, _USER, project.id, "dist/hand-edited.js", "file-legacy"
    )

    overlay = await durability.sync_project_files(
        _WS, _USER, project.id, _VM, client=_FakeVm({"a.ts": b"A"}), uploads=uploads
    )

    assert overlay == {"a.ts": "file-1", "dist/hand-edited.js": "file-legacy"}


async def test_an_in_tab_project_is_never_pruned() -> None:
    """The in-tab runtime's baseline lives in the BROWSER, so its store is partial.

    It holds only the files the tab wrote; the starter scaffold is re-materialized
    client-side and never stored. Pruning "everything not in the store" there would
    delete the entire scaffold, so the prune is gated on a store that a sync has
    actually verified against a workspace.
    """
    project = await _project()
    uploads = _FakeUploads()
    await durability.put_project_file(
        _WS, _USER, project.id, "src/App.tsx", "MINE", uploads=uploads
    )
    assert (await codeproject_service.get_project(_WS, _USER, project.id)).overlay_complete is False

    # Opening it on the VM runtime: the scaffold is materialized, then restored over.
    fresh = _FakeVm({"src/App.tsx": b"TEMPLATE", "vite.config.ts": b"TEMPLATE-CONFIG"})
    await durability.restore_project(
        _WS, _USER, project.id, _FRESH_VM, client=fresh, uploads=uploads
    )

    assert fresh.files["src/App.tsx"] == b"MINE"  # the tab's copy wins
    assert fresh.files["vite.config.ts"] == b"TEMPLATE-CONFIG"  # and nothing was pruned
    assert not any(c.startswith("rm -f ") for c in fresh.exec_calls)


async def test_a_restore_that_materialized_nothing_prunes_nothing() -> None:
    """A store whose blobs are all unreadable must not delete the baseline.

    The replay tolerates one bad entry, so a total failure looks like "0 replayed" —
    and pruning on top of that would remove the clone the user could still work from.
    """
    project = await _project()
    uploads = _FakeUploads()
    vm = _FakeVm({"a.ts": b"A"})
    await durability.sync_project_files(_WS, _USER, project.id, _VM, client=vm, uploads=uploads)

    # Every blob vanishes from storage (a reap, a bucket misconfiguration).
    uploads._blobs.clear()

    fresh = _FakeVm({"a.ts": b"A", "b.ts": b"B"})
    await durability.restore_project(
        _WS, _USER, project.id, _FRESH_VM, client=fresh, uploads=uploads
    )

    assert fresh.files == {"a.ts": b"A", "b.ts": b"B"}
    assert not any(c.startswith("rm -f ") for c in fresh.exec_calls)


# ---------------------------------------------------------------------------
# Caps — sized for a whole source tree now, and still LOUD rather than partial.
# ---------------------------------------------------------------------------


async def test_the_caps_hold_an_ordinary_source_project() -> None:
    """The re-tune, stated as the behaviour: the defaults must not reject real work.

    The old 10 MB / 2000-file limits were sized for a DELTA. The store now holds the
    whole tree, and a 3000-file project is ordinary — under the old numbers it would
    have 413'd on every capture.
    """
    project = await _project()
    uploads = _FakeUploads()
    vm = _FakeVm({f"src/mod{i}.ts": b"x" * 2048 for i in range(3000)})

    overlay = await durability.sync_project_files(
        _WS, _USER, project.id, _VM, client=vm, uploads=uploads
    )
    assert len(overlay) == 3000


async def test_a_project_over_the_file_cap_fails_loud(monkeypatch) -> None:  # noqa: ANN001
    project = await _project()
    uploads = _FakeUploads()
    vm = _FakeVm({"a.ts": b"A", "b.ts": b"B"})
    monkeypatch.setenv("POCKETPAW_CODEPROJECT_OVERLAY_MAX_FILES", "1")

    with pytest.raises(CloudError) as exc:
        await durability.sync_project_files(_WS, _USER, project.id, _VM, client=vm, uploads=uploads)
    assert exc.value.code == "codeproject.overlay_too_many_files"
    assert exc.value.status_code == 413
    # Loud, and nothing was written — never a truncated store.
    assert uploads.upload_calls == []
    assert (await codeproject_service.get_project(_WS, _USER, project.id)).overlay == {}


async def test_a_project_over_the_byte_cap_fails_loud(monkeypatch) -> None:  # noqa: ANN001
    project = await _project()
    uploads = _FakeUploads()
    vm = _FakeVm({"big.ts": b"x" * 4096})
    monkeypatch.setenv("POCKETPAW_CODEPROJECT_OVERLAY_MAX_MB", "0.001")  # ~1 KB

    with pytest.raises(CloudError) as exc:
        await durability.sync_project_files(_WS, _USER, project.id, _VM, client=vm, uploads=uploads)
    assert exc.value.code == "codeproject.overlay_too_large"
    assert uploads.upload_calls == []
