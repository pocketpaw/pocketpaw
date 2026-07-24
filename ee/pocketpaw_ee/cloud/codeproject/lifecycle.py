# lifecycle.py — Code Mode "open a project" orchestration (CM-2a, CM-2a′).
# Created 2026-07-16 (feat/code-mode): the durable-project → ephemeral-sandbox
# resolver. It sits ABOVE ``codeproject/service.py`` (the registry) and drives the
# runtime through ``websandbox/provision.py``. Per ee/cloud Rule 2 it NEVER touches
# either Beanie doc directly — the project read/bind goes through ``codeproject.
# service`` and the sandbox read/provision through ``websandbox.service`` /
# ``websandbox.provision``. Living here (not in service.py) keeps the service the
# sole doc writer and avoids a service→provision import cycle.
#
# One flow — ``open_project``: resolve the project (tenant-scoped) → if it's bound
# to a sandbox that is still LIVE (``ready`` with a Daytona id), reuse it; else
# cold-provision a fresh sandbox for the project's repo (idempotent on the repo, so
# a reprovision reuses the same WebSandbox row) and rebind. Returns the ready
# sandbox view for the caller to connect to.
#
# 2026-07-16 (CM-2a′): the reprovision branch now RESTORES the row's durable S3
# snapshot into the fresh VM. The WebSandbox row is stable across reprovisions
# (idempotent on the repo), so a ``snapshot_file_id`` written on a prior
# disconnect survives on the row; when we boot a fresh VM we overlay that snapshot
# so a returning user picks up their uncommitted work + branch instead of a bare
# re-clone. Best-effort: a restore failure leaves the fresh clone usable rather
# than blocking the open.
#
# 2026-07-17 (reopen-stale-vm fix): reuse now PROBES Daytona before trusting the
# row. The Mongo row's ``status`` is not authoritative about the VM: Daytona's own
# aggressive lifecycle (stop 5 min → delete-on-stop) destroys an idle Code Mode VM
# long before our 30-min idle reaper reconciles the row, so a ``ready`` row can
# point at a Daytona sandbox that no longer exists. Reopening a day-old project
# then bound the editor to a dead VM (no connection, empty tree). ``_reuse_if_live``
# now confirms the bound VM actually exists and is ``started`` in Daytona; anything
# else falls through to reprovision + snapshot restore.
#
# 2026-07-25 (B2, feat/code-daytona-project-anchor): restore now reads the durable
# state off the PROJECT, not off the sandbox row. The old anchor was a bug waiting
# to fire: a WebSandbox row is unique per (workspace, user, repo), and a scaffold
# project puts a TEMPLATE id in ``repo``, so every project built from the same
# starter shared ONE row and would have stomped each other's snapshot + overlay.
# Anchoring on the project id removes that by construction and makes the stated
# "CodeProject = durable / WebSandbox = ephemeral" split true rather than
# aspirational. ``open_project`` therefore restores via ``restore_project`` into the
# freshly-provisioned VM's Daytona id.
#
# The same change adds the one-way rollout backfill: ``_backfill_legacy_durability``
# lifts a repo project's legacy sandbox-keyed pointers onto the project before
# anything reads them. It runs LAZILY here rather than as a boot-time sweep because
# (a) this is the only moment the legacy state matters — it has to be on the project
# before the first project-keyed restore or mirror, and an open is exactly when that
# happens; (b) it already has the tenancy context (workspace + user + owned project)
# a global sweep would have to invent; and (c) it is idempotent by construction, so
# every later open re-checks for free instead of needing a one-shot migration to be
# scheduled, monitored, and re-run. It sits at the TOP of ``open_project`` so the
# reuse branch is covered too, and it never fires for scaffold projects — copying
# one shared row's state into N sibling starter projects is the very stomping this
# task removes.
from __future__ import annotations

import contextlib
import logging

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.codeproject.domain import CodeProjectView, is_scaffold_provider
from pocketpaw_ee.cloud.daytona.client import DaytonaClient, get_daytona_client
from pocketpaw_ee.cloud.websandbox import durability as websandbox_durability
from pocketpaw_ee.cloud.websandbox import provision as websandbox_provision
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.domain import WebSandboxView

logger = logging.getLogger(__name__)

# The sandbox states that count as "live enough to reuse" — a ready row with a
# bound Daytona id. Anything else (opening, stopped, reaped, or no bound VM) means
# the runtime is gone and we reprovision.
_REUSABLE_STATUS = "ready"

# The one Daytona state a bound VM must be in to reuse it directly. A ``stopped``
# or ``archived`` VM (if not already deleted) can't serve the terminal / file RPC,
# and a Code Mode VM is delete-on-stop, so anything but ``started`` means "gone or
# unusable" → reprovision from the durable project instead of resuming a stale VM.
_LIVE_VM_STATE = "started"


async def open_project(
    workspace_id: str,
    user_id: str,
    project_id: str,
    client: DaytonaClient | None = None,
) -> WebSandboxView:
    """Open a durable project, returning a ready sandbox to connect to.

    Flow:
      1. Load the project (tenant + owner scoped; ``NotFound`` if not owned).
      2. If it's bound to a sandbox that is still live (``ready`` + Daytona id),
         reuse it and re-stamp ``last_opened_at``.
      3. Otherwise cold-provision a fresh sandbox for the project's repo (via
         ``websandbox.provision.open_sandbox`` — idempotent on the repo, so this
         reuses the project's stable WebSandbox row and boots a new VM into it),
         then restore the PROJECT's durable state into it and bind it.

    Returns the ready ``WebSandboxView``. The bound-but-invalid case (the VM was
    reaped, or the id no longer resolves) falls through to reprovision — "if the
    id is invalid or unavailable, make a new sandbox."
    """
    project = await codeproject_service.get_project(workspace_id, user_id, project_id)
    # B2 rollout: lift any legacy sandbox-keyed durable state onto the project
    # BEFORE anything reads it. Idempotent, so this is a cheap no-op on every
    # subsequent open.
    project = await _backfill_legacy_durability(workspace_id, user_id, project)

    reused = await _reuse_if_live(workspace_id, user_id, project, client)
    if reused is not None:
        # Re-stamp last-opened so the projects grid orders by real recency; the
        # bound id is unchanged, so this is just a touch.
        await codeproject_service.bind_current_sandbox(workspace_id, user_id, project_id, reused.id)
        return reused

    # Provision a fresh sandbox for the repo (idempotent on the repo → reuses the
    # project's stable WebSandbox row, boots a new VM into it) and bind it.
    sandbox = await websandbox_provision.open_sandbox(
        workspace_id, user_id, {"repo": project.repo}, client=client
    )
    # CM-2a′ / B2: overlay the PROJECT's durable snapshot (uncommitted work +
    # branch from a prior session) onto the freshly-cloned VM, if one exists.
    await _restore_if_snapshotted(workspace_id, user_id, project, sandbox, client)
    await codeproject_service.bind_current_sandbox(workspace_id, user_id, project_id, sandbox.id)
    logger.info(
        "codeproject.open: project=%s bound fresh sandbox=%s (repo=%s)",
        project_id,
        sandbox.id,
        project.repo,
    )
    return sandbox


async def delete_project(
    workspace_id: str,
    user_id: str,
    project_id: str,
    client: DaytonaClient | None = None,
) -> None:
    """Delete a durable project and best-effort tear down its bound sandbox VM.

    Flow:
      1. Resolve the project (tenant + owner scoped; ``NotFound`` if not owned).
      2. If it's bound to a sandbox, best-effort stop+delete the Daytona VM and
         mark the WebSandbox row ``reaped`` so no runtime is orphaned.
      3. Delete the durable CodeProject doc (the sole doc write goes through the
         service).

    The VM teardown is best-effort: a Daytona hiccup must not block removing the
    project (the idle reaper + Daytona's own delete-on-stop reclaim a stray VM
    anyway). The doc delete is the authoritative step.
    """
    project = await codeproject_service.get_project(workspace_id, user_id, project_id)

    if project.current_sandbox_id:
        await _teardown_bound_sandbox(workspace_id, user_id, project.current_sandbox_id, client)

    await codeproject_service.delete_project(workspace_id, user_id, project_id)
    logger.info("codeproject.delete: project=%s removed (repo=%s)", project_id, project.repo)


async def _teardown_bound_sandbox(
    workspace_id: str,
    user_id: str,
    sandbox_row_id: str,
    client: DaytonaClient | None,
) -> None:
    """Best-effort stop+delete the bound Daytona VM and mark its row reaped.

    Every step is guarded: a missing row, an unconfigured/erroring Daytona, or a
    VM that's already gone are all fine — the goal is just "don't leak a live VM
    when the project is deleted", not to fail the delete on a teardown miss.
    """
    try:
        sandbox = await websandbox_service.get_sandbox(workspace_id, user_id, sandbox_row_id)
    except NotFound:
        return
    daytona = client if client is not None else get_daytona_client()
    if daytona is not None and sandbox.sandbox_id:
        with contextlib.suppress(Exception):
            await daytona.stop_sandbox(sandbox.sandbox_id)
        with contextlib.suppress(Exception):
            await daytona.delete_sandbox(sandbox.sandbox_id)
    with contextlib.suppress(Exception):
        await websandbox_service.mark_reaped(sandbox_row_id)


async def _restore_if_snapshotted(
    workspace_id: str,
    user_id: str,
    project: CodeProjectView,
    sandbox: WebSandboxView,
    client: DaytonaClient | None,
) -> None:
    """Restore the PROJECT's durable state into the freshly-provisioned VM, if any.

    Both durability tiers live on the durable project row (B2): the
    ``snapshot_file_id`` (captured on a prior clean disconnect) AND the
    write-through ``overlay`` (per-file edits mirrored since the last snapshot —
    the tier that covers a crash / idle-out before any disconnect snapshot).
    Restore fires when EITHER exists; a project with neither (first open, or
    nothing edited) is a clean no-op.

    The anchor moved off the WebSandbox row deliberately. That row is unique per
    (workspace, user, repo), so N projects sharing a starter template shared one
    row and one set of pointers; reading the project instead makes each project's
    durable state its own. The row is still what tells us WHICH VM to untar into —
    ``sandbox.sandbox_id``, the live Daytona id.

    Best-effort by design: a restore failure (VM gone, S3 down, corrupt tarball)
    is logged and swallowed — the fresh clone from ``open_sandbox`` is still a
    usable workspace, so a durability miss must never fail the open.
    """
    if not project.snapshot_file_id and not project.overlay:
        return
    if not sandbox.sandbox_id:
        # No bound VM to untar into (provision handed back an unready row). The
        # durable state stays on the project untouched for the next open.
        logger.warning(
            "codeproject.open: project=%s has durable state but sandbox=%s has no VM; "
            "skipping restore",
            project.id,
            sandbox.id,
        )
        return
    try:
        await websandbox_durability.restore_project(
            workspace_id, user_id, project.id, sandbox.sandbox_id, client=client
        )
        logger.info(
            "codeproject.open: restored durable state for project=%s into daytona=%s "
            "(snapshot=%s, overlay=%d file(s))",
            project.id,
            sandbox.sandbox_id,
            project.snapshot_file_id,
            len(project.overlay or {}),
        )
    except Exception:  # noqa: BLE001 — a fresh clone is still usable; never block open
        logger.warning(
            "codeproject.open: snapshot restore failed for project=%s (snapshot=%s); "
            "continuing with the fresh clone",
            project.id,
            project.snapshot_file_id,
            exc_info=True,
        )


async def _backfill_legacy_durability(
    workspace_id: str,
    user_id: str,
    project: CodeProjectView,
) -> CodeProjectView:
    """Lift legacy sandbox-keyed durable state onto the project. Returns the view.

    The one-way rollout migration for B2. Before the cutover, a project's
    uncommitted work was recorded on the WebSandbox row it was bound to; after it,
    restore reads the project. Without this copy, a returning user with real work
    on the old anchor reopens to a bare re-clone — silent data loss at exactly the
    moment durability is supposed to pay off.

    Conditions, all of which make it safe to run on every open:
      * the project must hold NO durable state of its own (the service enforces
        this too, so a race can't clobber fresher work);
      * the project must be bound to a resolvable, owned sandbox row carrying
        something to adopt;
      * the project must NOT be a scaffold project. Its ``repo`` is a template id,
        so its sandbox row is shared with every sibling built from the same
        starter — adopting that row's state would copy one project's files into N
        others, which is the exact stomping this cutover removes. Scaffold projects
        never persisted through this path anyway (the Daytona adapter refuses a
        non-repo source), so there is nothing to lose by skipping them.

    Best-effort: any failure returns the project unchanged rather than blocking the
    open. The legacy fields are left in place, so a missed backfill is retried on
    the next open instead of being lost.
    """
    if project.snapshot_file_id or project.overlay:
        return project
    if not project.current_sandbox_id or is_scaffold_provider(project.provider):
        return project
    try:
        sandbox = await websandbox_service.get_sandbox(
            workspace_id, user_id, project.current_sandbox_id
        )
    except NotFound:
        return project
    if not sandbox.snapshot_file_id and not sandbox.overlay:
        return project
    try:
        adopted = await codeproject_service.adopt_legacy_durability(
            workspace_id,
            user_id,
            project.id,
            sandbox.snapshot_file_id,
            dict(sandbox.overlay or {}),
        )
    except Exception:  # noqa: BLE001 — a backfill miss must never block the open
        logger.warning(
            "codeproject.open: legacy durability backfill failed for project=%s "
            "(sandbox=%s); continuing without it",
            project.id,
            project.current_sandbox_id,
            exc_info=True,
        )
        return project
    logger.info(
        "codeproject.open: adopted legacy durable state for project=%s from sandbox=%s "
        "(snapshot=%s, overlay=%d file(s))",
        project.id,
        project.current_sandbox_id,
        adopted.snapshot_file_id,
        len(adopted.overlay or {}),
    )
    return adopted


async def _reuse_if_live(
    workspace_id: str,
    user_id: str,
    project: CodeProjectView,
    client: DaytonaClient | None,
) -> WebSandboxView | None:
    """Return the project's bound sandbox iff it's still live, else None.

    "Live" == a ready row with a bound Daytona id WHOSE VM still exists and is
    ``started`` in Daytona. A missing bound id, an unresolvable/foreign row
    (``NotFound``), any non-``ready`` row status, OR a bound VM that Daytona has
    already destroyed/stopped all mean the runtime is gone and the caller should
    reprovision.

    The Daytona probe is what fixes reopening a stale project: the row's status
    lags the VM's real lifecycle (Daytona deletes an idle VM in minutes; our
    reaper reconciles the row on a 30-min sweep), so trusting the row alone bound
    the editor to a dead VM.
    """
    if not project.current_sandbox_id:
        return None
    try:
        sandbox = await websandbox_service.get_sandbox(
            workspace_id, user_id, project.current_sandbox_id
        )
    except NotFound:
        return None
    if sandbox.status != _REUSABLE_STATUS or not sandbox.sandbox_id:
        return None
    if not await _daytona_vm_is_live(sandbox.sandbox_id, client):
        return None
    return sandbox


async def _daytona_vm_is_live(sandbox_id: str, client: DaytonaClient | None) -> bool:
    """True iff the Daytona VM ``sandbox_id`` still exists and is ``started``.

    Fail-safe toward reprovision: if Daytona is unconfigured, the id no longer
    resolves (deleted/reaped out-of-band), or the VM is in any state but
    ``started``, return False so the caller boots a fresh VM and restores the
    durable snapshot rather than handing back a dead runtime. A probe error is
    treated as "not live" — a spurious reprovision is recoverable; binding a dead
    VM is the actual outage.
    """
    daytona = client if client is not None else get_daytona_client()
    if daytona is None:
        logger.info(
            "codeproject.open: Daytona unavailable — cannot verify VM %s, reprovisioning",
            sandbox_id,
        )
        return False
    try:
        info = await daytona.get_sandbox_by_id(sandbox_id)
    except Exception:  # noqa: BLE001 — any lookup failure means the VM is unreachable/gone
        logger.info(
            "codeproject.open: bound VM %s no longer resolves in Daytona — reprovisioning",
            sandbox_id,
        )
        return False
    state = (getattr(info, "state", "") or "").lower()
    if state == _LIVE_VM_STATE:
        return True
    logger.info(
        "codeproject.open: bound VM %s is %r (not %s) — reprovisioning",
        sandbox_id,
        state,
        _LIVE_VM_STATE,
    )
    return False


__all__ = ["delete_project", "open_project"]
