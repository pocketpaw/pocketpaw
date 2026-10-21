# ee/pocketpaw_ee/cloud/belt/router.py — the Belt & Pulley console REST surface.
#
# The /belt pages build against exactly these endpoints:
#   * GET  /belt/repos          — discover git repos under the allowlist roots
#   * POST /belt/repos {path}   — add a new repo root (admin/owner-gated)
#   * POST /belt/repos/init     — CREATE a new git repo under an allowlist root
#                                 (admin-gated); optional GitHub remote through the
#                                 injectable ``RepoCreator`` (tests fake ``gh``)
#   * GET  /belt/runs           — list this workspace's station runs (newest-first)
#   * GET  /belt/runs/{action_id} — one run + its proposed diff (capped ~200 KB)
#   * GET  /belt/runs/{action_id}/feed?stage= — what one station step stored, as
#                                 chat-shaped ``steps``
#   * GET  /belt/runs/{action_id}/stream?after= — the run's feed live, as SSE
#                                 (``service.open_run_stream`` has the replay rules)
#   * GET  /belt/runs/{action_id}/blueprint — the bound repo's C4 model where the
#                                 run starts, with the run's files joined to it
#   * GET  /belt/runs/{action_id}/screenshots/{name} — one of the run's kept
#                                 snapshot PNGs (``image/png``), only a name the
#                                 run lists
#   * POST|GET /belt/runs/{action_id}/try — open the run's CI-built app on this
#                                 Mac (start 202 / join 200), and poll its state
#   * POST|GET /belt/runs/{action_id}/reviews — record a review (multipart), list
#                                 them; GET .../reviews/{review_id}/video and
#                                 .../frames/{t_ms}.png serve its media
#                                 (``belt/review.py`` has the rules)
#   * GET  /belt/blueprints?repo= — one repo's app blueprint graph: apps, features,
#                                 components, journeys, gaps (``app_blueprint_read``)
#   * GET  /belt/blueprints/c4?repo=&app= — one app's planned C4 model with a
#                                 status per component
#   * GET  /belt/blueprints/suite-map?repo= — the suite feature map (layers, apps,
#                                 features with adopters) with each row's live status
#   * GET  /belt/architecture?repo= — one repo's committed C4 model at its trunk
#                                 for the architecture viewer (``architecture_read``)
#
# Routes are THIN: they read identity (workspace + user) from the cloud deps,
# delegate to ``ee.cloud.belt.service``, and return what the service built. RBAC
# is ``require_action_any_workspace``: ``belt.read`` (MEMBER) on the reads and
# on trying and reviewing a run, ``belt.manage`` (ADMIN) on the repo mutations.
# A service ``BeltConsoleError`` is translated to a ``CloudError`` here (never
# ``HTTPException``) whose body also carries ``detail`` (the sentence the UI
# shows), so a foreign or non-belt run is the same 404 on every run route.

"""FastAPI router for the Belt & Pulley console (repos + runs)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from pocketpaw_ee.cloud._core.deps import (
    current_user_id,
    current_workspace_id,
    require_action_any_workspace,
    require_paid_or_early_access_allowed,
)
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.belt import app_blueprint_read, architecture_read
from pocketpaw_ee.cloud.belt import review as belt_review
from pocketpaw_ee.cloud.belt import service as belt_service
from pocketpaw_ee.cloud.license import require_license

# Belt runs spend off the credit ledger, so an unpaid early-access workspace is
# refused on every route here (403 ``early_access.not_available``).
router = APIRouter(
    prefix="/belt",
    tags=["Belt"],
    dependencies=[Depends(require_license), Depends(require_paid_or_early_access_allowed)],
)


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


def review_room_dep() -> belt_review.ReviewRoom:
    """The Review Room's side effects (subprocess runner, background tries).
    Tests override this to fake gh, tar and ``open``."""
    return belt_review.ReviewRoom()


class _BeltError(CloudError):
    """The cloud envelope plus ``detail``: the UI's ``HttpError`` reads its
    sentence from ``detail``, never from ``error.message``."""

    def to_dict(self) -> dict:
        return {**super().to_dict(), "detail": self.message}


def _to_cloud_error(exc: belt_service.BeltConsoleError) -> CloudError:
    """Map a service ``BeltConsoleError`` to the cloud error envelope.

    The service's own ``code`` wins; otherwise the status drives it so the wire
    stays stable: 400 → ``belt.invalid_repo``, 404 → ``belt.run_not_found``,
    anything else → ``belt.error``. The human message is the service's (already
    safe to show — no path content beyond what the user submitted)."""
    if exc.code:
        return _BeltError(exc.status_code, exc.code, exc.message)
    if exc.status_code == 404:
        return _BeltError(404, "belt.run_not_found", exc.message)
    if exc.status_code == 400:
        return _BeltError(400, "belt.invalid_repo", exc.message)
    return _BeltError(exc.status_code, "belt.error", exc.message)


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


@router.get("/runs/{action_id}/stream")
async def get_run_stream(
    action_id: str,
    after: str = Query(default="0", max_length=64),
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> StreamingResponse:
    """The run's feed as it happens (``text/event-stream``): ``start``,
    ``stage``, ``thinking`` / ``tool_start`` / ``tool_result`` and a terminal
    ``stream_end``, each ``id`` a cursor to resume ``after``. A foreign or
    non-belt run is a 404 before the stream opens."""
    try:
        body = await belt_service.open_run_stream(workspace_id, action_id, after)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc
    return StreamingResponse(
        body,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs/{action_id}/feed")
async def get_run_feed(
    action_id: str,
    stage: str = Query(default="develop", pattern=r"^[a-z]{1,20}$"),
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """What one station step of a run did: ``{action_id, stage, steps?,
    stepsOmitted?}``, the steps in the chat history wire shape. No ``steps``
    key when the stage recorded none; a foreign or non-belt run is a 404."""
    try:
        return await belt_service.get_run_feed(workspace_id, action_id, stage)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc


@router.get("/runs/{action_id}/blueprint")
async def get_run_blueprint(
    action_id: str,
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """The run on its line's blueprint: ``{action_id, ref, model, files}``,
    ``model`` the bound repo's C4 model (``paths`` globs included) at the
    run's base or ``null``, ``files`` the run's touched files as ``{path,
    component}``. A foreign or non-belt run is a 404."""
    try:
        return await belt_service.get_run_blueprint(workspace_id, action_id)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc


@router.get("/blueprints")
async def get_app_blueprint(
    repo: str = Query(min_length=1),
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """The repo's app blueprint graph (design doc 2026-10-15, section 5), cached
    60 s. 404 ``belt.repo_not_found`` outside the allowlist; a failed refresh
    serves the last graph with ``stale: true``, 503 when there is none yet."""
    try:
        return await app_blueprint_read.read_blueprint(workspace_id, repo)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc


@router.get("/blueprints/c4")
async def get_app_blueprint_c4(
    repo: str = Query(min_length=1),
    app: str = Query(pattern=r"^[a-z0-9][a-z0-9-]*$"),
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """One app's planned C4 view: ``{app, model: {scope, model}, status}``,
    ``status`` mapping each component id to planned, landed, changing or
    failed. A bad ``app`` is 422; an app with no model is 404."""
    try:
        return await app_blueprint_read.read_c4(workspace_id, repo, app)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc


@router.get("/blueprints/suite-map")
async def get_suite_map(
    repo: str = Query(min_length=1),
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """The repo's suite feature map joined to the blueprint's live statuses,
    off the same 60 s cache. 404 ``belt.suite_map_not_found`` when dev has no
    readable ``docs/blueprints/suite/feature-map.json``."""
    try:
        return await app_blueprint_read.read_suite_map(workspace_id, repo)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc


@router.get("/architecture")
async def get_architecture(
    repo: str = Query(min_length=1),
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> dict[str, Any]:
    """The repo's C4 model at ``origin/dev`` (``main`` without one):
    ``{repo, slug, sha, stale, read_at, model, flows, loom, live}``. Fresh for
    60 s, then served from cache while a background read refreshes it; the
    first read returns ``live: null`` and fills it in behind. 404
    ``belt.architecture_not_found`` without a readable model; a failed refresh
    serves the last body with ``stale: true``, 503 when there is none yet."""
    try:
        return await architecture_read.read_architecture(workspace_id, repo)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc


@router.get("/runs/{action_id}/screenshots/{name}")
async def get_run_screenshot(
    action_id: str,
    name: str,
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> FileResponse:
    """One of the run's snapshot PNGs, as listed in its ``screenshots``. A
    foreign or non-belt run, or any name the run does not list, is a 404."""
    try:
        path = await belt_service.get_run_screenshot(workspace_id, action_id, name)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc
    return FileResponse(
        path,
        media_type="image/png",
        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=300"},
    )


@router.post("/runs/{action_id}/try")
async def start_run_try(
    action_id: str,
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
    room: belt_review.ReviewRoom = Depends(review_room_dep),
) -> JSONResponse:
    """Open the run's CI-built app on this Mac: 202 with a ``preparing``
    ``TryState`` when a try starts, 200 with the current one when a try is
    already preparing or opened. 409 ``belt.no_artifact``, 501
    ``belt.try_unsupported`` (not a Mac, or GitHub unreadable)."""
    try:
        status, state = await room.start_try(workspace_id, action_id)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc
    return JSONResponse(state, status_code=status)


@router.get("/runs/{action_id}/try")
async def get_run_try(
    action_id: str,
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
    room: belt_review.ReviewRoom = Depends(review_room_dep),
) -> dict[str, Any]:
    """The run's latest ``TryState`` (``idle`` when none was started)."""
    try:
        return await room.get_try(workspace_id, action_id)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc


@router.post("/runs/{action_id}/reviews")
async def post_run_review(
    action_id: str,
    request: Request,
    user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
    room: belt_review.ReviewRoom = Depends(review_room_dep),
) -> JSONResponse:
    """Record a review (multipart: ``verdict``, ``notes`` JSON, ``video``,
    ``frames``): 201 with the ``Review``, 200 replaying the one an
    ``Idempotency-Key`` already stored. ``by`` is the caller, never a field."""
    email = str(getattr(user, "email", "") or "")
    who = str(getattr(user, "full_name", "") or "") or "a teammate"
    try:
        status, review = await room.submit_review(
            workspace_id,
            action_id,
            by=f"user:{email or user.id}",
            who=who,
            headers=request.headers,
            stream=request.stream(),
            idempotency_key=request.headers.get("idempotency-key", ""),
        )
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc
    return JSONResponse(review, status_code=status)


@router.get("/runs/{action_id}/reviews")
async def list_run_reviews(
    action_id: str,
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> list[dict[str, Any]]:
    """The run's reviews, newest first (``[]`` when none)."""
    try:
        return await belt_review.list_reviews(workspace_id, action_id)
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc


_IMMUTABLE = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, max-age=31536000, immutable",
}


@router.get("/runs/{action_id}/reviews/{review_id}/video")
async def get_run_review_video(
    action_id: str,
    review_id: str,
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> FileResponse:
    """A review's recording (``video/webm``; ``Range`` gets a 206)."""
    try:
        path = await belt_review.review_media(workspace_id, action_id, review_id, "video.webm")
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc
    return FileResponse(path, media_type="video/webm", headers=_IMMUTABLE)


@router.get("/runs/{action_id}/reviews/{review_id}/frames/{t_ms}.png")
async def get_run_review_frame(
    action_id: str,
    review_id: str,
    t_ms: str,
    _user: Any = Depends(require_action_any_workspace("belt.read")),
    workspace_id: str = Depends(current_workspace_id),
) -> FileResponse:
    """A review's still at ``t_ms`` (``image/png``)."""
    try:
        path = await belt_review.review_media(workspace_id, action_id, review_id, f"{t_ms}.png")
    except belt_service.BeltConsoleError as exc:
        raise _to_cloud_error(exc) from exc
    return FileResponse(path, media_type="image/png", headers=_IMMUTABLE)


__all__ = ["router"]
