# test_codeproject.py — service + lifecycle tests for the Code Mode durable-project
# registry (CM-2a, feat/code-mode).
#
# The registry runs on real Beanie over mongomock-motor (the ``mongo_db`` fixture)
# so the tenant-filtered query paths are exercised for real; the sandbox runtime is
# the injected FAKE DaytonaClient from the provision suite, so no test touches real
# Daytona.
#
# Covers:
#   * create_project is idempotent per (workspace, user, provider, repo) and
#     defaults the display name to the repo's short name.
#   * tenancy fail-closed: get_project / list_projects never cross the
#     workspace OR user boundary; a foreign id reads as NotFound.
#   * open_project provisions a fresh sandbox when the project has none and binds
#     it (current_sandbox_id + last_opened_at).
#   * open_project REUSES a still-live (ready) bound sandbox instead of
#     provisioning a second one.
#   * open_project REPROVISIONS when the bound sandbox is invalid/unavailable
#     (reaped) — "if the id is invalid or unavailable, make a new sandbox."
#   * (CM-2a′) open_project RESTORES the row's durable snapshot into the fresh VM
#     on reprovision when one exists, and skips restore when there's no snapshot;
#     a restore failure is swallowed (the fresh clone is still returned ready).
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.codeproject import lifecycle, service
from pocketpaw_ee.cloud.websandbox import service as sandbox_service

from tests.cloud.test_websandbox_provision import _FakeDaytonaClient

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws-1"
_USER = "user-1"
_REPO = "https://github.com/acme/widgets.git"


# ---------------------------------------------------------------------------
# create / idempotency / naming.
# ---------------------------------------------------------------------------


async def test_create_project_defaults_name_to_repo_short_name() -> None:
    view = await service.create_project(_WS, _USER, {"repo": _REPO})
    assert view.name == "widgets"  # ".git" stripped, last path segment
    assert view.provider == "github"
    assert view.repo == _REPO
    assert view.current_sandbox_id is None
    assert view.snapshot_file_id is None


async def test_create_project_is_idempotent_per_repo() -> None:
    first = await service.create_project(_WS, _USER, {"repo": _REPO, "name": "Widgets"})
    second = await service.create_project(_WS, _USER, {"repo": _REPO})
    # Same durable id — a returning user lands back on the same project, and the
    # second create does not clobber the existing name.
    assert second.id == first.id
    assert second.name == "Widgets"
    listing = await service.list_projects(_WS, _USER)
    assert len(listing) == 1


async def test_create_project_explicit_name_is_kept() -> None:
    view = await service.create_project(_WS, _USER, {"repo": _REPO, "name": "My Thing"})
    assert view.name == "My Thing"


# ---------------------------------------------------------------------------
# tenancy fail-closed.
# ---------------------------------------------------------------------------


async def test_get_project_is_owner_scoped() -> None:
    mine = await service.create_project(_WS, _USER, {"repo": _REPO})
    # Same workspace, different user → NotFound (not another user's project).
    with pytest.raises(NotFound):
        await service.get_project(_WS, "user-2", mine.id)


async def test_get_project_is_workspace_scoped() -> None:
    mine = await service.create_project(_WS, _USER, {"repo": _REPO})
    # Different workspace, same user id → NotFound.
    with pytest.raises(NotFound):
        await service.get_project("ws-2", _USER, mine.id)


async def test_list_projects_never_crosses_tenant() -> None:
    await service.create_project(_WS, _USER, {"repo": _REPO})
    await service.create_project(_WS, "user-2", {"repo": "https://github.com/a/b"})
    await service.create_project("ws-2", _USER, {"repo": "https://github.com/c/d"})
    mine = await service.list_projects(_WS, _USER)
    assert len(mine) == 1
    assert mine[0].repo == _REPO


async def test_get_project_missing_id_is_notfound() -> None:
    with pytest.raises(NotFound):
        await service.get_project(_WS, _USER, "not-a-real-id")


# ---------------------------------------------------------------------------
# open_project — provision / reuse / reprovision.
# ---------------------------------------------------------------------------


async def test_open_project_provisions_and_binds_when_unbound() -> None:
    project = await service.create_project(_WS, _USER, {"repo": _REPO})
    fake = _FakeDaytonaClient()

    sandbox = await lifecycle.open_project(_WS, _USER, project.id, client=fake)

    # A VM was cold-provisioned and the returned sandbox is ready + bound.
    assert len(fake.create_calls) == 1
    assert sandbox.status == "ready"
    assert sandbox.sandbox_id is not None

    # The durable project now points at that sandbox row and stamped last_opened.
    reloaded = await service.get_project(_WS, _USER, project.id)
    assert reloaded.current_sandbox_id == sandbox.id
    assert reloaded.last_opened_at is not None


async def test_open_project_reuses_live_bound_sandbox() -> None:
    project = await service.create_project(_WS, _USER, {"repo": _REPO})
    fake = _FakeDaytonaClient()

    first = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    # Second open with the SAME (still-ready) sandbox must NOT provision again.
    second = await lifecycle.open_project(_WS, _USER, project.id, client=fake)

    assert second.id == first.id
    assert len(fake.create_calls) == 1  # reused, not re-provisioned


async def test_open_project_reprovisions_when_bound_sandbox_reaped() -> None:
    project = await service.create_project(_WS, _USER, {"repo": _REPO})
    fake = _FakeDaytonaClient()

    first = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    # The bound VM gets reaped out from under the project (system reaper terminal).
    await sandbox_service.mark_reaped(first.id)

    # Opening again finds the bound row no longer live → provisions a fresh VM.
    second = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    assert len(fake.create_calls) == 2  # reprovisioned
    assert second.status == "ready"
    # Same stable WebSandbox row (idempotent on the repo), freshly booted.
    assert second.id == first.id


# ---------------------------------------------------------------------------
# rename / delete.
# ---------------------------------------------------------------------------


async def test_rename_project_changes_name() -> None:
    project = await service.create_project(_WS, _USER, {"repo": _REPO, "name": "Old"})
    renamed = await service.rename_project(_WS, _USER, project.id, {"name": "  New Name  "})
    assert renamed.name == "New Name"  # trimmed
    reloaded = await service.get_project(_WS, _USER, project.id)
    assert reloaded.name == "New Name"


async def test_rename_project_is_owner_scoped() -> None:
    mine = await service.create_project(_WS, _USER, {"repo": _REPO})
    with pytest.raises(NotFound):
        await service.rename_project(_WS, "user-2", mine.id, {"name": "Hijacked"})


async def test_delete_project_removes_row_and_tears_down_vm() -> None:
    project = await service.create_project(_WS, _USER, {"repo": _REPO})
    fake = _FakeDaytonaClient()
    sandbox = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    assert sandbox.sandbox_id is not None

    await lifecycle.delete_project(_WS, _USER, project.id, client=fake)

    # The durable project is gone…
    with pytest.raises(NotFound):
        await service.get_project(_WS, _USER, project.id)
    # …and the bound VM was torn down (not left to leak).
    assert sandbox.sandbox_id in fake.delete_calls


async def test_delete_project_is_owner_scoped() -> None:
    mine = await service.create_project(_WS, _USER, {"repo": _REPO})
    fake = _FakeDaytonaClient()
    with pytest.raises(NotFound):
        await lifecycle.delete_project(_WS, "user-2", mine.id, client=fake)
    # Still there for the real owner.
    assert (await service.get_project(_WS, _USER, mine.id)).id == mine.id


async def test_open_project_reprovisions_when_vm_deleted_out_of_band() -> None:
    """The reported bug: a day-old project's row still reads ``ready`` but Daytona
    already deleted the VM (delete-on-stop) before the 30-min reaper reconciled the
    row. Reuse must PROBE Daytona, see the VM is gone, and reprovision — not hand
    back a dead sandbox that connects to nothing."""
    project = await service.create_project(_WS, _USER, {"repo": _REPO})
    fake = _FakeDaytonaClient()

    first = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    # The row is left ``ready`` (the reaper never ran), but Daytona reclaimed the
    # VM on its own — get_sandbox_by_id will now raise for that id.
    assert first.sandbox_id is not None
    fake.deleted_out_of_band.add(first.sandbox_id)

    second = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    assert len(fake.create_calls) == 2  # dead VM detected → reprovisioned
    assert second.status == "ready"
    assert second.sandbox_id is not None
    assert second.sandbox_id != first.sandbox_id  # a genuinely fresh VM


# ---------------------------------------------------------------------------
# open_project — CM-2a′ snapshot restore on reprovision.
# ---------------------------------------------------------------------------


async def test_open_project_restores_snapshot_on_reprovision(monkeypatch) -> None:
    project = await service.create_project(_WS, _USER, {"repo": _REPO})
    fake = _FakeDaytonaClient()

    restored: list[dict] = []

    async def _spy_restore(workspace_id, user_id, row_id, *, client=None):  # noqa: ANN001
        restored.append({"workspace_id": workspace_id, "user_id": user_id, "row_id": row_id})

    monkeypatch.setattr(lifecycle.websandbox_durability, "restore_workspace", _spy_restore)

    # First open: a fresh row with no snapshot → nothing to restore.
    first = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    assert restored == []

    # A clean disconnect captured a snapshot onto the stable row; then the VM was
    # reaped out from under the project.
    await sandbox_service.set_snapshot(_WS, _USER, first.id, "file-123")
    await sandbox_service.mark_reaped(first.id)

    # Reopening reprovisions a fresh VM AND restores the row's snapshot into it.
    second = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    assert second.id == first.id
    assert len(restored) == 1
    assert restored[0]["row_id"] == second.id


async def test_open_project_restores_overlay_only_on_reprovision(monkeypatch) -> None:
    # The crash-before-any-snapshot case: the row carries a write-through overlay
    # but no snapshot. Restore must still fire.
    project = await service.create_project(_WS, _USER, {"repo": _REPO})
    fake = _FakeDaytonaClient()

    restored: list[str] = []

    async def _spy_restore(workspace_id, user_id, row_id, *, client=None):  # noqa: ANN001
        restored.append(row_id)

    monkeypatch.setattr(lifecycle.websandbox_durability, "restore_workspace", _spy_restore)

    first = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    await sandbox_service.set_overlay_entry(_WS, _USER, first.id, "a.ts", "file-1")
    await sandbox_service.mark_reaped(first.id)

    second = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    assert restored == [second.id]


async def test_open_project_swallows_restore_failure(monkeypatch) -> None:
    project = await service.create_project(_WS, _USER, {"repo": _REPO})
    fake = _FakeDaytonaClient()

    first = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    await sandbox_service.set_snapshot(_WS, _USER, first.id, "file-123")
    await sandbox_service.mark_reaped(first.id)

    async def _boom_restore(workspace_id, user_id, row_id, *, client=None):  # noqa: ANN001
        raise RuntimeError("boom: restore failed")

    monkeypatch.setattr(lifecycle.websandbox_durability, "restore_workspace", _boom_restore)

    # A restore failure is best-effort: the open still returns a ready sandbox
    # (the fresh clone), not an error.
    second = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    assert second.status == "ready"
    assert second.id == first.id
