# lifecycle.py — Code Mode "open a project" orchestration (CM-2a).
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
from __future__ import annotations

import logging

from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.codeproject.domain import CodeProjectView
from pocketpaw_ee.cloud.daytona.client import DaytonaClient
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
