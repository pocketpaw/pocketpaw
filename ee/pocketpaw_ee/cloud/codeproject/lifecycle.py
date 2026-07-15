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
from __future__ import annotations

import logging

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.codeproject.domain import CodeProjectView
from pocketpaw_ee.cloud.daytona.client import DaytonaClient
from pocketpaw_ee.cloud.websandbox import durability as websandbox_durability
from pocketpaw_ee.cloud.websandbox import provision as websandbox_provision
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.domain import WebSandboxView

logger = logging.getLogger(__name__)

# The sandbox states that count as "live enough to reuse" — a ready row with a
# bound Daytona id. Anything else (opening, stopped, reaped, or no bound VM) means
# the runtime is gone and we reprovision.
_REUSABLE_STATUS = "ready"


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

    reused = await _reuse_if_live(workspace_id, user_id, project)
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
    """Restore the sandbox row's durable S3 snapshot into its fresh VM, if any.

    The WebSandbox row is stable across reprovisions, so a ``snapshot_file_id``
    captured on a prior disconnect (see ``websandbox.ws.snapshot_on_disconnect``)
    is still on the row when we reprovision. A row with no snapshot (never
    disconnected with work, or first open) is a clean no-op.

    Best-effort by design: a restore failure (VM gone, S3 down, corrupt tarball)
    is logged and swallowed — the fresh clone from ``open_sandbox`` is still a
    usable workspace, so a durability miss must never fail the open.
    """
    if not sandbox.snapshot_file_id:
        return
    try:
        await websandbox_durability.restore_workspace(
            workspace_id, user_id, sandbox.id, client=client
        )
        logger.info(
            "codeproject.open: restored snapshot=%s into sandbox=%s",
            sandbox.snapshot_file_id,
            sandbox.id,
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
) -> WebSandboxView | None:
    """Return the project's bound sandbox iff it's still live, else None.

    "Live" == a ready row with a bound Daytona id. A missing bound id, an
    unresolvable/foreign row (``NotFound``), or any non-``ready`` status all mean
    the runtime is gone and the caller should reprovision.
    """
    if not project.current_sandbox_id:
        return None
    try:
        sandbox = await websandbox_service.get_sandbox(
            workspace_id, user_id, project.current_sandbox_id
        )
    except NotFound:
        return None
    if sandbox.status == _REUSABLE_STATUS and sandbox.sandbox_id:
        return sandbox
    return None


__all__ = ["open_project"]
