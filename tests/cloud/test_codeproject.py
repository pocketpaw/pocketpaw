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
#   * (CM-2a′, re-anchored in B2) open_project RESTORES the PROJECT's durable
#     snapshot into the fresh VM on reprovision when one exists, and skips restore
#     when there's no snapshot; a restore failure is swallowed (the fresh clone is
#     still returned ready).
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
# open_project — CM-2a′ snapshot restore on reprovision, PROJECT-anchored (B2).
# ---------------------------------------------------------------------------
#
# Adapted 2026-07-25 (B2, feat/code-daytona-project-anchor). These three tests
# asserted the same behaviour against the OLD anchor: durable state was staged on
# the WebSandbox row (``sandbox_service.set_snapshot`` / ``set_overlay_entry``) and
# restore went through ``restore_workspace(row_id)``. The behaviour under test is
# unchanged — restore fires on reprovision when either tier exists, and a failure
# is swallowed — only the anchor moved, so the staging now goes through the project
# service and the spy asserts the project id + the fresh VM's Daytona id.


async def test_open_project_restores_snapshot_on_reprovision(monkeypatch) -> None:
    project = await service.create_project(_WS, _USER, {"repo": _REPO})
    fake = _FakeDaytonaClient()

    restored: list[dict] = []

    async def _spy_restore(workspace_id, user_id, project_id, sandbox_id, *, client=None):  # noqa: ANN001
        restored.append(
            {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "project_id": project_id,
                "sandbox_id": sandbox_id,
            }
        )

    monkeypatch.setattr(lifecycle.websandbox_durability, "restore_project", _spy_restore)

    # First open: a fresh project with no snapshot → nothing to restore.
    first = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    assert restored == []

    # A clean disconnect captured a snapshot onto the durable PROJECT; then the VM
    # was reaped out from under it.
    await service.set_project_snapshot(_WS, _USER, project.id, "file-123")
    await sandbox_service.mark_reaped(first.id)

    # Reopening reprovisions a fresh VM AND restores the project's snapshot into it.
    second = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    assert second.id == first.id
    assert len(restored) == 1
    assert restored[0]["project_id"] == project.id
    # Restored into the FRESH VM, addressed by its Daytona id (not the row id).
    assert restored[0]["sandbox_id"] == second.sandbox_id


async def test_open_project_restores_overlay_only_on_reprovision(monkeypatch) -> None:
    # The crash-before-any-snapshot case: the project carries a write-through
    # overlay but no snapshot. Restore must still fire.
    project = await service.create_project(_WS, _USER, {"repo": _REPO})
    fake = _FakeDaytonaClient()

    restored: list[str] = []

    async def _spy_restore(workspace_id, user_id, project_id, sandbox_id, *, client=None):  # noqa: ANN001
        restored.append(project_id)

    monkeypatch.setattr(lifecycle.websandbox_durability, "restore_project", _spy_restore)

    first = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    await service.set_project_overlay_entry(_WS, _USER, project.id, "a.ts", "file-1")
    await sandbox_service.mark_reaped(first.id)

    await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    assert restored == [project.id]


async def test_open_project_swallows_restore_failure(monkeypatch) -> None:
    project = await service.create_project(_WS, _USER, {"repo": _REPO})
    fake = _FakeDaytonaClient()

    first = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    await service.set_project_snapshot(_WS, _USER, project.id, "file-123")
    await sandbox_service.mark_reaped(first.id)

    async def _boom_restore(workspace_id, user_id, project_id, sandbox_id, *, client=None):  # noqa: ANN001
        raise RuntimeError("boom: restore failed")

    monkeypatch.setattr(lifecycle.websandbox_durability, "restore_project", _boom_restore)

    # A restore failure is best-effort: the open still returns a ready sandbox
    # (the fresh clone), not an error.
    second = await lifecycle.open_project(_WS, _USER, project.id, client=fake)
    assert second.status == "ready"
    assert second.id == first.id


# ---------------------------------------------------------------------------
# starter projects — two prompts, two projects.
# ---------------------------------------------------------------------------
#
# Added 2026-07-22 reproducing a captain-reported bug: "when I try to create a
# project by prompt then I don't see that project in the project tab."
#
# The cause is that idempotency is keyed on ``(workspace, user, provider, repo)``
# and a STARTER project puts the starter id in ``repo``. For a git project that
# key is right — cloning github.com/acme/widgets twice is the same project. For a
# starter it conflates "which TEMPLATE" with "which PROJECT", and the catalog has
# only four entries (react / vue / svelte / next), so two unrelated prompts
# collide almost immediately.
#
# The user then gets navigated to the FIRST project, their new name is silently
# discarded, and the projects tab still shows one row.

_STARTER = "react"


async def test_two_prompts_on_the_same_starter_make_two_projects() -> None:
    """The bug, stated as the behaviour we want.

    "A todo app" and "a blog" both plan to the react starter. They are two
    different projects that happen to share a template.
    """
    todo = await service.create_project(
        _WS, _USER, {"repo": _STARTER, "provider": "starter", "name": "todo app"}
    )
    blog = await service.create_project(
        _WS, _USER, {"repo": _STARTER, "provider": "starter", "name": "blog"}
    )

    assert blog.id != todo.id, (
        "the second prompt returned the FIRST project — the registry key treats "
        "'which template' as 'which project', so every later prompt on this "
        "starter is swallowed"
    )
    assert blog.name == "blog", "the name the user chose was discarded"

    listing = await service.list_projects(_WS, _USER)
    assert len(listing) == 2, (
        f"the projects tab shows {len(listing)} project(s), not 2 — this is "
        "exactly what the user reports as 'I don't see that project'"
    )


async def test_a_git_repo_stays_idempotent() -> None:
    """The guard on the fix. Whatever makes starters distinct must NOT make a
    returning user's git clone mint a duplicate — that idempotency is deliberate
    and separately tested above."""
    first = await service.create_project(_WS, _USER, {"repo": _REPO, "name": "Widgets"})
    second = await service.create_project(_WS, _USER, {"repo": _REPO})

    assert second.id == first.id
    assert len(await service.list_projects(_WS, _USER)) == 1


# ---------------------------------------------------------------------------
# initial build prompt — persist on create, expose on the wire, mark consumed.
# ---------------------------------------------------------------------------
#
# A1 (feat/code-initial-prompt): creating a /code project from a description
# records WHAT to build on the durable row, exposes it on the wire so the frontend
# can auto-run one build turn on first open, and offers an owner-scoped op to latch
# it consumed on turn start (re-armable on a retry-build).


async def test_create_persists_initial_prompt_unconsumed_and_exposes_it() -> None:
    view = await service.create_project(
        _WS,
        _USER,
        {
            "repo": _STARTER,
            "provider": "starter",
            "name": "todo app",
            "initial_prompt": "build a todo app",
        },
    )
    assert view.initial_prompt == "build a todo app"
    assert view.initial_prompt_consumed is False

    # It survives a reload and reaches the camelCase wire response.
    reloaded = await service.get_project(_WS, _USER, view.id)
    assert reloaded.initial_prompt == "build a todo app"
    wire = service.view_to_wire(reloaded)
    assert wire.initialPrompt == "build a todo app"
    assert wire.initialPromptConsumed is False


async def test_create_without_initial_prompt_leaves_it_null() -> None:
    view = await service.create_project(_WS, _USER, {"repo": _REPO})
    assert view.initial_prompt is None
    assert view.initial_prompt_consumed is False
    assert service.view_to_wire(view).initialPrompt is None


async def test_github_idempotent_recreate_does_not_overwrite_prompt() -> None:
    first = await service.create_project(
        _WS, _USER, {"repo": _REPO, "initial_prompt": "the original prompt"}
    )
    # A second create for the same git repo is an idempotent hit — the existing
    # row is returned UNCHANGED, so a stray prompt on the re-create can't clobber it.
    second = await service.create_project(
        _WS, _USER, {"repo": _REPO, "initial_prompt": "a different prompt"}
    )
    assert second.id == first.id
    assert second.initial_prompt == "the original prompt"


async def test_mark_initial_prompt_consumed_sets_true() -> None:
    project = await service.create_project(
        _WS, _USER, {"repo": _STARTER, "provider": "starter", "initial_prompt": "build it"}
    )
    assert project.initial_prompt_consumed is False

    updated = await service.mark_initial_prompt_consumed(_WS, _USER, project.id, {})
    assert updated.initial_prompt_consumed is True
    # The prompt itself is untouched — only the flag flips.
    assert updated.initial_prompt == "build it"
    assert (await service.get_project(_WS, _USER, project.id)).initial_prompt_consumed is True


async def test_mark_initial_prompt_consumed_can_reset() -> None:
    project = await service.create_project(
        _WS, _USER, {"repo": _STARTER, "provider": "starter", "initial_prompt": "build it"}
    )
    await service.mark_initial_prompt_consumed(_WS, _USER, project.id, {"consumed": True})
    # A retry-build re-arms the prompt.
    rearmed = await service.mark_initial_prompt_consumed(
        _WS, _USER, project.id, {"consumed": False}
    )
    assert rearmed.initial_prompt_consumed is False


async def test_mark_initial_prompt_consumed_already_consumed_is_noop() -> None:
    project = await service.create_project(
        _WS, _USER, {"repo": _STARTER, "provider": "starter", "initial_prompt": "build it"}
    )
    once = await service.mark_initial_prompt_consumed(_WS, _USER, project.id, {"consumed": True})
    # Marking it consumed again is a clean no-op, not an error.
    twice = await service.mark_initial_prompt_consumed(_WS, _USER, project.id, {"consumed": True})
    assert once.initial_prompt_consumed is True
    assert twice.initial_prompt_consumed is True


async def test_mark_initial_prompt_consumed_is_owner_scoped() -> None:
    mine = await service.create_project(
        _WS, _USER, {"repo": _STARTER, "provider": "starter", "initial_prompt": "build it"}
    )
    # Another user in the same workspace can't consume my project's prompt.
    with pytest.raises(NotFound):
        await service.mark_initial_prompt_consumed(_WS, "user-2", mine.id, {"consumed": True})
    # A different workspace can't either.
    with pytest.raises(NotFound):
        await service.mark_initial_prompt_consumed("ws-2", _USER, mine.id, {"consumed": True})
    # Still un-consumed for the real owner — no cross-tenant write leaked through.
    assert (await service.get_project(_WS, _USER, mine.id)).initial_prompt_consumed is False
