# ee/pocketpaw_ee/cloud/belt/router.py
# Created: 2026-06-10 (feat/belt-console-backend, SC-1 + SC-2) — the Belt &
# Pulley console REST surface. The /belt page builds against exactly these
# endpoints (a sibling frontend PR pins the contract):
#   * GET  /belt/repos          — discover git repos under the allowlist roots
#   * POST /belt/repos {path}   — add a new repo root (admin/owner-gated)
#   * GET  /belt/runs           — list this workspace's station runs (newest-first)
#   * GET  /belt/runs/{action_id} — one run + its proposed diff (capped ~200 KB)
#
# Routes are THIN: they read identity (workspace + user) from the cloud deps,
# delegate to ``ee.cloud.belt.service``, and return the wire dict the service
# built. RBAC is enforced via ``require_action_any_workspace`` route deps —
# ``belt.read`` (MEMBER) on the read routes, ``belt.manage`` (ADMIN) on the
# add-repo route — mirroring how the instinct / connector / skills routers gate
# (the instinct router lives at ``pocketpaw_ee.instinct.router``; the gate
# pattern is the same). Errors propagate via ``CloudError`` so the central cloud
# error handler maps them to the JSON envelope — the router never raises
# ``HTTPException`` (entity rule 10). A service-level ``BeltConsoleError`` (the
# add-repo validation failures) is translated to a ``CloudError`` here.

"""FastAPI router for the Belt & Pulley console (repos + runs)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
)
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.belt import service as belt_service
from pocketpaw_ee.cloud.license import require_license

router = APIRouter(prefix="/belt", tags=["Belt"], dependencies=[Depends(require_license)])


class AddRepoRequest(BaseModel):
    """Body for ``POST /belt/repos`` — the repo path to authorize.

    ``path`` is an absolute filesystem path to an existing git repo. The service
    realpath-resolves it, confirms it's a git repo, and appends the RESOLVED
    path to the workspace's persisted allowlist extension.
    """

    path: str = Field(min_length=1)


def _to_cloud_error(exc: belt_service.BeltConsoleError) -> CloudError:
    """Map a service ``BeltConsoleError`` to the cloud error envelope.

    The service status code drives the machine-readable code so the wire stays
    stable: 400 → ``belt.invalid_repo``, 404 → ``belt.run_not_found``, anything
    else → ``belt.error``. The human message is the service's (already safe to
    show — no path content beyond what the user submitted)."""
    if exc.status_code == 404:
        return CloudError(404, "belt.run_not_found", exc.message)
    if exc.status_code == 400:
        return CloudError(400, "belt.invalid_repo", exc.message)
    return CloudError(exc.status_code, "belt.error", exc.message)


@router.get("/repos")
async def list_repos(
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Discover git repos under the workspace's allowlist roots, one level deep.

    Returns ``{"repos": [{path, name, current_branch, branches}, ...]}``. Any
    workspace member may read (``belt.read``)."""
    return await belt_service.discover_repos(workspace_id)


@router.post("/repos")
async def add_repo(
    body: AddRepoRequest,
    _user: Any = Depends(require_action_any_workspace("belt.manage")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """Authorize a new repo root for the workspace (admin/owner-gated,
    ``belt.manage``).

    Validates the path is an existing git repo, realpath-resolves it, and
    persists it to the workspace's allowlist extension. 4xx with a clear message
    on a non-existent / non-git / unresolvable path. Returns ``{"repo": {...}}``.
    """
    try:
        return await belt_service.add_repo(workspace_id, body.path)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc


@router.get("/runs")
async def list_runs(
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """List this workspace's Belt station runs, newest-first.

    Returns ``{"runs": [{action_id, task, summary, status, stage, repo,
    base_branch, branch?, pr_url?, created_at, correlation_id}, ...]}``. Any
    workspace member may read (``belt.read``)."""
    return await belt_service.list_runs(workspace_id)


@router.get("/runs/{action_id}")
async def get_run(
    action_id: str,
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
    _user_id: str = Depends(current_user_id),
) -> dict[str, Any]:
    """Return one run + its proposed diff (capped ~200 KB).

    A run from another workspace, or a non-belt Action, is a 404 (we never
    confirm a cross-tenant Action exists)."""
    try:
        return await belt_service.get_run(workspace_id, action_id)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc


__all__ = ["router"]
