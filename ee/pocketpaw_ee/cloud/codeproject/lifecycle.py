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
from __future__ import annotations

import logging

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.codeproject.domain import CodeProjectView
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
         then bind it onto the project.

    Returns the ready ``WebSandboxView``. The bound-but-invalid case (the VM was
    reaped, or the id no longer resolves) falls through to reprovision — "if the
    id is invalid or unavailable, make a new sandbox."
    """
    project = await codeproject_service.get_project(workspace_id, user_id, project_id)

    reused = await _reuse_if_live(workspace_id, user_id, project, client)
    if reused is not None:
        # Re-stamp last-opened so the projects grid orders by real recency; the
        # bound id is unchanged, so this is just a touch.
        await codeproject_service.bind_current_sandbox(
            workspace_id, user_id, project_id, reused.id
        )
        return reused

    # Provision a fresh sandbox for the repo (idempotent on the repo → reuses the
    # project's stable WebSandbox row, boots a new VM into it) and bind it.
    sandbox = await websandbox_provision.open_sandbox(
        workspace_id, user_id, {"repo": project.repo}, client=client
    )
    # CM-2a′: overlay the row's durable snapshot (uncommitted work + branch from a
    # prior session) onto the freshly-cloned VM, if one exists.
    await _restore_if_snapshotted(workspace_id, user_id, sandbox, client)
    await codeproject_service.bind_current_sandbox(
        workspace_id, user_id, project_id, sandbox.id
    )
    logger.info(
        "codeproject.open: project=%s bound fresh sandbox=%s (repo=%s)",
        project_id,
        sandbox.id,
        project.repo,
    )
    return sandbox


async def _restore_if_snapshotted(
    workspace_id: str,
    user_id: str,
    sandbox: WebSandboxView,
    client: DaytonaClient | None,
) -> None:
    """Restore the sandbox row's durable state into its fresh VM, if any.

    The WebSandbox row is stable across reprovisions, so both durability tiers
    survive on the row when we reprovision: the ``snapshot_file_id`` (captured on
    a prior clean disconnect) AND the write-through ``overlay`` (per-file edits
    mirrored since the last snapshot — the tier that covers a crash / idle-out
    before any disconnect snapshot). Restore fires when EITHER exists; a row with
    neither (first open, or nothing edited) is a clean no-op.

    Best-effort by design: a restore failure (VM gone, S3 down, corrupt tarball)
    is logged and swallowed — the fresh clone from ``open_sandbox`` is still a
    usable workspace, so a durability miss must never fail the open.
    """
    if not sandbox.snapshot_file_id and not sandbox.overlay:
        return
    try:
        await websandbox_durability.restore_workspace(
            workspace_id, user_id, sandbox.id, client=client
        )
        logger.info(
            "codeproject.open: restored durable state into sandbox=%s "
            "(snapshot=%s, overlay=%d file(s))",
            sandbox.id,
            sandbox.snapshot_file_id,
            len(sandbox.overlay or {}),
        )
    except Exception:  # noqa: BLE001 — a fresh clone is still usable; never block open
        logger.warning(
            "codeproject.open: snapshot restore failed for sandbox=%s (snapshot=%s); "
            "continuing with the fresh clone",
            sandbox.id,
            sandbox.snapshot_file_id,
            exc_info=True,
        )


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


__all__ = ["open_project"]
