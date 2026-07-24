# test_codeproject_daytona_anchor.py — the B2 cutover: the DAYTONA runtime's
# durability is anchored on the durable CodeProject, not on the ephemeral
# WebSandbox row.
#
# Created 2026-07-25 (B2, feat/code-daytona-project-anchor).
#
# The bug this removes: a WebSandbox row is unique per (workspace, user, repo),
# and a SCAFFOLD project puts a TEMPLATE id in ``repo``. Every project built from
# the same starter therefore shared ONE row — and, before this change, one
# ``snapshot_file_id`` + one ``overlay``. Two projects would have overwritten each
# other's durable state. Anchoring on the project id removes that by construction.
#
# What is proved here, end to end, on real Beanie over mongomock-motor (the
# ``mongo_db`` fixture) with FAKE Daytona + FAKE blob storage injected through the
# existing DI seams:
#   * the full chain stays intact and moves whole: provision -> mirror a write ->
#     snapshot on disconnect -> reprovision -> restore, with every pointer read
#     from the CodeProject and the WebSandbox row's durable fields never written.
#   * the collision is gone: two projects sharing one starter ``repo`` (and even
#     one sandbox ROW) hold independent durable state.
#   * the degrade path: a sandbox with NO owning project still mirrors and
#     snapshots against the row exactly as before — no write is dropped.
#   * the rollout backfill: a repo project with legacy state on its sandbox row
#     adopts it on open, is idempotent on a second open, is non-destructive to the
#     row, never overwrites state the project already holds, and never fires for a
#     scaffold project (whose row is shared).
from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud.codeproject import lifecycle
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.websandbox import durability
from pocketpaw_ee.cloud.websandbox import provision as websandbox_provision
from pocketpaw_ee.cloud.websandbox import service as sandbox_service
from pocketpaw_ee.cloud.websandbox import ws as terminal_ws

from tests.cloud.test_codeproject_durability import _FakeDaytonaClient as _FakeVmClient
from tests.cloud.test_codeproject_durability import _FakeUploads
from tests.cloud.test_websandbox_provision import _FakeDaytonaClient as _FakeProvisionClient

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws-1"
_USER = "user-1"
_REPO = "https://github.com/acme/widgets.git"
_STARTER = "react"
_SNAPSHOT_TMP = "/tmp/ws-snapshot.tgz"  # noqa: S108 — a sandbox VM path, not host


@pytest.fixture()
def uploads(monkeypatch) -> _FakeUploads:  # noqa: ANN001
    """A fake blob store wired in as the module default.

    ``snapshot_on_disconnect`` and ``restore_project`` (via ``open_project``) build
    their own uploads service, so the DI seam has to be closed at the module level
    for a whole-chain test. One instance carries the blobs, so bytes written by a
    mirror or a snapshot are readable by the later restore.
    """
    fake = _FakeUploads()
    monkeypatch.setattr(durability, "build_uploads", lambda: fake)
    return fake


async def _starter_row():
    """Register the runtime row a starter project would get.

    Registered directly rather than through ``provision.open_sandbox`` because the
    provisioner only accepts an http(s) git URL — scaffold-on-Daytona is a separate
    task. What matters here is the ROW's key: it is (workspace, user, repo), and
    ``repo`` is the template id, so every project on that starter resolves to this
    one row.
    """
    return await sandbox_service.create_sandbox(
        _WS, _USER, {"repo": _STARTER, "sandbox_id": "dtn-shared", "status": "ready"}
    )


# ---------------------------------------------------------------------------
# Proof 1 — the whole chain, project-anchored.
# ---------------------------------------------------------------------------


async def test_full_chain_mirrors_snapshots_and_restores_against_the_project(uploads) -> None:  # noqa: ANN001
    """provision -> mirror -> snapshot on disconnect -> reprovision -> restore.

    Every durable pointer is read off the CodeProject; the WebSandbox row's
    ``snapshot_file_id`` / ``overlay`` are never written, so nothing in the chain
    depends on the ephemeral row surviving.
    """
    project = await codeproject_service.create_project(_WS, _USER, {"repo": _REPO})
    prov = _FakeProvisionClient()

    # 1. Open the project — cold-provisions a VM and binds the row.
    sandbox = await lifecycle.open_project(_WS, _USER, project.id, client=prov)
    assert sandbox.sandbox_id is not None

    # The terminal socket knows only its ROW; it resolves the durable anchor.
    project_id = await terminal_ws.resolve_owning_project(_WS, _USER, sandbox.id)
    assert project_id == project.id

    # 2. An editor save flows through the socket's write-through hook.
    on_write, _on_delete, _on_move = terminal_ws.build_durability_hooks(
        _WS, _USER, sandbox.id, project_id, uploads
    )
    await on_write("src/app.ts", b"EDITED-IN-THE-VM")

    stored = await codeproject_service.get_project(_WS, _USER, project.id)
    assert list(stored.overlay) == ["src/app.ts"]
    row = await sandbox_service.get_sandbox(_WS, _USER, sandbox.id)
    assert row.overlay == {}, "the mirror still wrote to the EPHEMERAL row"

    # 3. A clean disconnect snapshots the live VM onto the project.
    vm = _FakeVmClient(tar_bytes=b"SNAPSHOT-TAR")
    await terminal_ws.snapshot_on_disconnect(
        _WS,
        _USER,
        sandbox.id,
        vm,
        project_id=project_id,
        daytona_id=sandbox.sandbox_id,
    )
    snapshotted = await codeproject_service.get_project(_WS, _USER, project.id)
    assert snapshotted.snapshot_file_id is not None
    assert snapshotted.overlay == {}, "a full snapshot supersedes the overlay"
    row = await sandbox_service.get_sandbox(_WS, _USER, sandbox.id)
    assert row.snapshot_file_id is None, "the snapshot pointer landed on the EPHEMERAL row"

    # 4. The VM is reaped; reopening reprovisions and restores from the PROJECT.
    await sandbox_service.mark_reaped(sandbox.id)
    second = await lifecycle.open_project(_WS, _USER, project.id, client=prov)
    assert second.sandbox_id != sandbox.sandbox_id  # genuinely fresh VM

    untarred = [c for c in prov.upload_calls if c["path"] == _SNAPSHOT_TMP]
    assert untarred, "the project's snapshot was never pushed into the fresh VM"
    assert untarred[-1]["data"] == b"SNAPSHOT-TAR"
    assert untarred[-1]["id"] == second.sandbox_id


async def test_delete_and_move_hooks_also_target_the_project(uploads) -> None:  # noqa: ANN001
    """The delete / rename sides of the overlay move with the write side."""
    project = await codeproject_service.create_project(_WS, _USER, {"repo": _REPO})
    prov = _FakeProvisionClient()
    sandbox = await lifecycle.open_project(_WS, _USER, project.id, client=prov)

    on_write, on_delete, on_move = terminal_ws.build_durability_hooks(
        _WS, _USER, sandbox.id, project.id, uploads
    )
    await on_write("a.ts", b"A")
    await on_write("dir/b.ts", b"B")

    await on_move("a.ts", "renamed.ts")
    await on_delete("dir")

    stored = await codeproject_service.get_project(_WS, _USER, project.id)
    assert list(stored.overlay) == ["renamed.ts"]
    row = await sandbox_service.get_sandbox(_WS, _USER, sandbox.id)
    assert row.overlay == {}


# ---------------------------------------------------------------------------
# Proof 2 — the collision is gone.
# ---------------------------------------------------------------------------


async def test_two_projects_on_one_starter_row_hold_independent_state(uploads) -> None:  # noqa: ANN001
    """The bug, stated as the behaviour we want.

    Two projects built from the same starter share ONE WebSandbox row (it is keyed
    (workspace, user, repo) and ``repo`` is a template id). Their durable state must
    still be their own.
    """
    todo = await codeproject_service.create_project(
        _WS, _USER, {"repo": _STARTER, "provider": "starter", "name": "todo"}
    )
    blog = await codeproject_service.create_project(
        _WS, _USER, {"repo": _STARTER, "provider": "starter", "name": "blog"}
    )
    assert todo.id != blog.id

    # One shared runtime row, both projects bound to it — the collision, exactly.
    # ``create_sandbox`` is idempotent on (workspace, user, repo), so registering
    # each project's runtime hands back the SAME row: that is the collision, not a
    # contrivance of the test.
    shared = await _starter_row()
    assert (await _starter_row()).id == shared.id

    await codeproject_service.bind_current_sandbox(_WS, _USER, todo.id, shared.id)
    await codeproject_service.bind_current_sandbox(_WS, _USER, blog.id, shared.id)

    todo_write, _d, _m = terminal_ws.build_durability_hooks(_WS, _USER, shared.id, todo.id, uploads)
    blog_write, _d2, _m2 = terminal_ws.build_durability_hooks(
        _WS, _USER, shared.id, blog.id, uploads
    )
    await todo_write("todo.ts", b"T")
    await blog_write("blog.ts", b"B")

    todo_state = await codeproject_service.get_project(_WS, _USER, todo.id)
    blog_state = await codeproject_service.get_project(_WS, _USER, blog.id)
    assert list(todo_state.overlay) == ["todo.ts"]
    assert list(blog_state.overlay) == ["blog.ts"], (
        "one project's files landed on the other — the durable state is still shared"
    )

    # And the shared ephemeral row — the thing they used to collide on — holds none.
    row = await sandbox_service.get_sandbox(_WS, _USER, shared.id)
    assert row.overlay == {}
    assert row.snapshot_file_id is None


async def test_shared_row_resolves_to_the_most_recently_opened_project() -> None:
    """When a row is shared, the socket belongs to the project just opened.

    ``open_project`` re-stamps ``last_opened_at`` immediately before the browser
    connects, so "most recently opened" is what identifies the editor on the other
    end of this socket.
    """
    todo = await codeproject_service.create_project(
        _WS, _USER, {"repo": _STARTER, "provider": "starter", "name": "todo"}
    )
    blog = await codeproject_service.create_project(
        _WS, _USER, {"repo": _STARTER, "provider": "starter", "name": "blog"}
    )
    shared = await _starter_row()

    await codeproject_service.bind_current_sandbox(_WS, _USER, todo.id, shared.id)
    # BSON datetimes are millisecond-precision, and a real user's two opens are
    # seconds apart — step past the tick so the test asserts ordering, not a tie.
    await asyncio.sleep(0.005)
    await codeproject_service.bind_current_sandbox(_WS, _USER, blog.id, shared.id)
    assert await terminal_ws.resolve_owning_project(_WS, _USER, shared.id) == blog.id

    # Reopening the first project moves the anchor back to it.
    await asyncio.sleep(0.005)
    await codeproject_service.bind_current_sandbox(_WS, _USER, todo.id, shared.id)
    assert await terminal_ws.resolve_owning_project(_WS, _USER, shared.id) == todo.id


async def test_resolve_owning_project_is_tenant_and_owner_scoped() -> None:
    project = await codeproject_service.create_project(_WS, _USER, {"repo": _REPO})
    sandbox = await websandbox_provision.open_sandbox(
        _WS, _USER, {"repo": _REPO}, client=_FakeProvisionClient()
    )
    await codeproject_service.bind_current_sandbox(_WS, _USER, project.id, sandbox.id)

    assert await terminal_ws.resolve_owning_project(_WS, _USER, sandbox.id) == project.id
    # Another user / another workspace can't resolve someone else's project.
    assert await terminal_ws.resolve_owning_project(_WS, "user-2", sandbox.id) is None
    assert await terminal_ws.resolve_owning_project("ws-2", _USER, sandbox.id) is None


# ---------------------------------------------------------------------------
# Proof 3 — degrade, never drop: a sandbox with no owning project.
# ---------------------------------------------------------------------------


async def test_sandbox_without_a_project_keeps_the_sandbox_keyed_behaviour(uploads) -> None:  # noqa: ANN001
    """A sandbox opened outside the project flow still mirrors and snapshots.

    Falling back to the older anchor persists the user's edits; skipping the write
    would lose them. That trade is the whole reason the degrade path exists.
    """
    prov = _FakeProvisionClient()
    sandbox = await websandbox_provision.open_sandbox(_WS, _USER, {"repo": _REPO}, client=prov)
    assert sandbox.sandbox_id is not None

    project_id = await terminal_ws.resolve_owning_project(_WS, _USER, sandbox.id)
    assert project_id is None  # nothing owns this row

    on_write, on_delete, on_move = terminal_ws.build_durability_hooks(
        _WS, _USER, sandbox.id, project_id, uploads
    )
    await on_write("a.ts", b"A")
    await on_write("gone.ts", b"G")
    await on_move("a.ts", "b.ts")
    await on_delete("gone.ts")

    row = await sandbox_service.get_sandbox(_WS, _USER, sandbox.id)
    assert list(row.overlay) == ["b.ts"], "the write was dropped instead of degrading"

    # The disconnect snapshot degrades the same way — onto the row.
    vm = _FakeVmClient(tar_bytes=b"ROW-TAR")
    await terminal_ws.snapshot_on_disconnect(_WS, _USER, sandbox.id, vm, project_id=None)
    row = await sandbox_service.get_sandbox(_WS, _USER, sandbox.id)
    assert row.snapshot_file_id is not None
    assert row.overlay == {}


async def test_snapshot_degrades_when_the_row_has_no_bound_vm(uploads) -> None:  # noqa: ANN001
    """A project id with no Daytona id can't address a VM — fall back, don't skip."""
    project = await codeproject_service.create_project(_WS, _USER, {"repo": _REPO})
    sandbox = await websandbox_provision.open_sandbox(
        _WS, _USER, {"repo": _REPO}, client=_FakeProvisionClient()
    )
    await codeproject_service.bind_current_sandbox(_WS, _USER, project.id, sandbox.id)

    vm = _FakeVmClient(tar_bytes=b"ROW-TAR")
    await terminal_ws.snapshot_on_disconnect(
        _WS, _USER, sandbox.id, vm, project_id=project.id, daytona_id=None
    )
    # Landed on the row (the sandbox-keyed path), not silently nowhere.
    row = await sandbox_service.get_sandbox(_WS, _USER, sandbox.id)
    assert row.snapshot_file_id is not None
    assert (await codeproject_service.get_project(_WS, _USER, project.id)).snapshot_file_id is None


# ---------------------------------------------------------------------------
# Proof 4 — the rollout backfill.
# ---------------------------------------------------------------------------


async def _bind_legacy_state(
    project_id: str,
    prov: _FakeProvisionClient,
    *,
    snapshot: str = "legacy-snap",
    overlay: tuple[str, str] = ("legacy.ts", "legacy-file"),
) -> str:
    """Open a project, then stage pre-cutover durable state on its sandbox ROW."""
    sandbox = await lifecycle.open_project(_WS, _USER, project_id, client=prov)
    # set_snapshot clears the overlay, so the snapshot goes first (the same order
    # the legacy runtime produced: snapshot on disconnect, then edits after it).
    await sandbox_service.set_snapshot(_WS, _USER, sandbox.id, snapshot)
    await sandbox_service.set_overlay_entry(_WS, _USER, sandbox.id, overlay[0], overlay[1])
    await sandbox_service.mark_reaped(sandbox.id)
    return sandbox.id


async def test_backfill_adopts_legacy_sandbox_state_on_open(monkeypatch) -> None:  # noqa: ANN001
    project = await codeproject_service.create_project(_WS, _USER, {"repo": _REPO})
    prov = _FakeProvisionClient()
    row_id = await _bind_legacy_state(project.id, prov)

    restored: list[str] = []

    async def _spy_restore(workspace_id, user_id, project_id, sandbox_id, *, client=None):  # noqa: ANN001
        restored.append(project_id)

    monkeypatch.setattr(lifecycle.websandbox_durability, "restore_project", _spy_restore)

    await lifecycle.open_project(_WS, _USER, project.id, client=prov)

    adopted = await codeproject_service.get_project(_WS, _USER, project.id)
    assert adopted.snapshot_file_id == "legacy-snap"
    assert adopted.overlay == {"legacy.ts": "legacy-file"}
    # …and the adopted state is what the reprovision restored from.
    assert restored == [project.id]

    # Non-destructive: the legacy fields stay readable for the rollout window.
    row = await sandbox_service.get_sandbox(_WS, _USER, row_id)
    assert row.snapshot_file_id == "legacy-snap"
    assert row.overlay == {"legacy.ts": "legacy-file"}


async def test_backfill_is_idempotent_across_opens(monkeypatch) -> None:  # noqa: ANN001
    project = await codeproject_service.create_project(_WS, _USER, {"repo": _REPO})
    prov = _FakeProvisionClient()
    await _bind_legacy_state(project.id, prov)

    async def _noop_restore(workspace_id, user_id, project_id, sandbox_id, *, client=None):  # noqa: ANN001
        return None

    monkeypatch.setattr(lifecycle.websandbox_durability, "restore_project", _noop_restore)

    await lifecycle.open_project(_WS, _USER, project.id, client=prov)
    first = await codeproject_service.get_project(_WS, _USER, project.id)

    # A second open re-runs the backfill check and must change nothing.
    await lifecycle.open_project(_WS, _USER, project.id, client=prov)
    second = await codeproject_service.get_project(_WS, _USER, project.id)

    assert second.snapshot_file_id == first.snapshot_file_id == "legacy-snap"
    assert second.overlay == first.overlay == {"legacy.ts": "legacy-file"}


async def test_backfill_never_overwrites_state_the_project_already_holds(monkeypatch) -> None:  # noqa: ANN001
    project = await codeproject_service.create_project(_WS, _USER, {"repo": _REPO})
    prov = _FakeProvisionClient()
    await _bind_legacy_state(project.id, prov)

    # The project has already snapshotted since the cutover — fresher than the row.
    await codeproject_service.set_project_snapshot(_WS, _USER, project.id, "fresh-snap")
    await codeproject_service.set_project_overlay_entry(
        _WS, _USER, project.id, "new.ts", "new-file"
    )

    async def _noop_restore(workspace_id, user_id, project_id, sandbox_id, *, client=None):  # noqa: ANN001
        return None

    monkeypatch.setattr(lifecycle.websandbox_durability, "restore_project", _noop_restore)

    await lifecycle.open_project(_WS, _USER, project.id, client=prov)

    kept = await codeproject_service.get_project(_WS, _USER, project.id)
    assert kept.snapshot_file_id == "fresh-snap", "a stale legacy pointer overwrote live work"
    assert kept.overlay == {"new.ts": "new-file"}


async def test_backfill_skips_scaffold_projects(monkeypatch) -> None:  # noqa: ANN001
    """A scaffold project's row is SHARED with every sibling on the same starter.

    Adopting it would copy one project's files into N others — the exact stomping
    this cutover removes. Scaffold projects never persisted through the Daytona
    path anyway, so there is nothing to carry across.
    """
    project = await codeproject_service.create_project(
        _WS, _USER, {"repo": _STARTER, "provider": "starter", "name": "todo"}
    )
    sandbox = await _starter_row()
    await codeproject_service.bind_current_sandbox(_WS, _USER, project.id, sandbox.id)
    await sandbox_service.set_snapshot(_WS, _USER, sandbox.id, "legacy-snap")

    async def _noop_restore(workspace_id, user_id, project_id, sandbox_id, *, client=None):  # noqa: ANN001
        return None

    monkeypatch.setattr(lifecycle.websandbox_durability, "restore_project", _noop_restore)

    await lifecycle.open_project(_WS, _USER, project.id, client=_FakeProvisionClient())

    fresh = await codeproject_service.get_project(_WS, _USER, project.id)
    assert fresh.snapshot_file_id is None
    assert fresh.overlay == {}


async def test_adopt_legacy_durability_is_owner_scoped() -> None:
    from pocketpaw_ee.cloud._core.errors import NotFound

    mine = await codeproject_service.create_project(_WS, _USER, {"repo": _REPO})
    with pytest.raises(NotFound):
        await codeproject_service.adopt_legacy_durability(
            _WS, "user-2", mine.id, "hijacked-snap", {"x.ts": "hijacked"}
        )
    assert (await codeproject_service.get_project(_WS, _USER, mine.id)).snapshot_file_id is None
