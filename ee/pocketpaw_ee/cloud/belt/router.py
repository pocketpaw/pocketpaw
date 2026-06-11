# ee/pocketpaw_ee/cloud/belt/router.py
# Created: 2026-06-10 (feat/belt-console-backend, SC-1 + SC-2) — the Belt &
# Pulley console REST surface. The /belt page builds against exactly these
# endpoints (a sibling frontend PR pins the contract):
#   * GET  /belt/repos          — discover git repos under the allowlist roots
#   * POST /belt/repos {path}   — add a new repo root (admin/owner-gated)
#   * POST /belt/repos/init     — CREATE a new git repo under an allowlist root
#                                 (admin-gated); optional GitHub remote
#   * GET  /belt/runs           — list this workspace's station runs (newest-first)
#   * GET  /belt/runs/{action_id} — one run + its proposed diff (capped ~200 KB)
#
# Updated: 2026-06-11 (feat/belt-repo-init) — added ``POST /belt/repos/init``.
# Same ADMIN gate as the add-repo mutation (``belt.manage``) and the same
# realpath discipline (the service validates the name + location_root). The
# ``RepoCreator`` is injected via a dependency so tests can fake the ``gh repo
# create`` shell-out; production gets the default ``GhCliRepoCreator``.
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


class InitRepoRequest(BaseModel):
    """Body for ``POST /belt/repos/init`` — create a brand-new git repository.

    * ``name`` — the new repo's directory name. Must be a safe single segment
      (``[a-z0-9._-]``, no path separators); the service validates it.
    * ``location_root`` — an existing directory UNDER an allowlist root the new
      repo dir is created in. The service realpath-resolves it and requires
      containment.
    * ``create_remote`` — when true, also create + push a private GitHub remote
      via ``gh repo create``. A remote failure keeps the local repo and returns
      a ``remote_error`` message; the local init is never rolled back.
    """

    name: str = Field(min_length=1)
    location_root: str = Field(min_length=1)
    create_remote: bool = False


def repo_creator_dep() -> belt_service.RepoCreator | None:
    """Provide the ``RepoCreator`` for the init route. Production returns ``None``
    so the service uses its default ``GhCliRepoCreator``; tests override this
    dependency to inject a fake that records the ``gh repo create`` args without
    touching GitHub (mirrors how the executor's ``PrOpener`` is faked)."""
    return None


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


@router.post("/repos/init")
async def init_repo(
    body: InitRepoRequest,
    _user: Any = Depends(require_action_any_workspace("belt.manage")),
    workspace_id: str = Depends(current_workspace_id),
    repo_creator: belt_service.RepoCreator | None = Depends(repo_creator_dep),
) -> dict[str, Any]:
    """Create a brand-new git repository under an allowlist root (admin/owner-
    gated, ``belt.manage``).

    Validates ``name`` is a safe directory segment and ``location_root`` resolves
    UNDER an authorized allowlist root, refuses an already-existing target,
    ``git init`` + seeds a README + initial commit (so the repo has a HEAD and a
    default branch), and registers the new repo via the same persistence the
    add-repo route uses. Returns ``{"repo": {path, name, current_branch,
    branches}}`` — the same repo shape the registry returns.

    With ``create_remote=true`` the route also creates + pushes a private GitHub
    remote via ``gh repo create``. IMPORTANT: a remote-creation failure does NOT
    roll back the local repo — it returns 200 with the repo plus a top-level
    ``remote_error`` message (the frontend shows it inline) so the local work is
    never lost.

    4xx with a clear, path-free message on: an invalid name (not a safe
    dirname), a ``location_root`` outside the allowlist, an already-existing
    target dir, or a git-init / commit failure.
    """
    try:
        return await belt_service.init_repo(
            workspace_id,
            name=body.name,
            location_root=body.location_root,
            create_remote=body.create_remote,
            repo_creator=repo_creator,
        )
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
