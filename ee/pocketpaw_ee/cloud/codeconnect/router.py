# router.py — Thin FastAPI adapter for the Code Mode GitHub connect flow (CM-3).
# Created 2026-07-16 (feat/code-mode): workspace+user scoped
# /api/v1/codeconnect. Tenancy for the authenticated routes comes from the
# RequestContext (never the body/query); connect.py does the work and CloudError ->
# JSON is handled by _core.http — this router never raises HTTPException.
#
# The ``/github/callback`` route is the exception: it's a top-level BROWSER redirect
# from GitHub after an install, so it carries NO bearer token and CANNOT use the
# RequestContext. It authenticates ENTIRELY via the signed ``state`` token minted at
# install-URL time (connect.handle_callback verifies it), then 302-redirects the
# browser back to the SPA. It stays license-gated (a server-side gate) like the rest.

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Depends, Query
from fastapi.responses import RedirectResponse

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import CloudError, Forbidden
from pocketpaw_ee.cloud.codeconnect import connect, service
from pocketpaw_ee.cloud.codeconnect.dto import (
    CodeConnectionListResponse,
    InstallUrlResponse,
    RepoListResponse,
    RepoResponse,
)
from pocketpaw_ee.cloud.license import require_license

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/codeconnect",
    tags=["CodeConnect"],
    dependencies=[Depends(require_license)],
)


def _require_workspace(ctx: RequestContext) -> str:
    """A workspace-scoped route needs an active workspace; fail closed if absent."""
    if not ctx.workspace_id:
        raise Forbidden("codeconnect.no_workspace", "No active workspace")
    return ctx.workspace_id


def _frontend_root() -> str:
    """The SPA origin the post-install callback redirects the browser back to."""
    return os.environ.get("POCKETPAW_FRONTEND_BASE_URL", "/").rstrip("/") or "/"


@router.get("/github/install-url", response_model=InstallUrlResponse)
async def github_install_url(
    ctx: RequestContext = Depends(request_context),
) -> InstallUrlResponse:
    """Return the GitHub App install URL (carrying a signed state) to open."""
    workspace_id = _require_workspace(ctx)
    url = connect.build_install_url(workspace_id, ctx.user_id)
    return InstallUrlResponse(url=url)


@router.get("/github/callback")
async def github_callback(
    installation_id: str = Query(""),
    state: str = Query(""),
    setup_action: str = Query(""),
) -> RedirectResponse:
    """Handle the post-install redirect: persist the connection, bounce to the SPA.

    Authenticated by the signed ``state`` alone (no bearer token on a browser
    redirect). On success or failure it always 302s back to ``/code`` with a status
    query param, so the user lands in the app rather than on a raw JSON error.
    """
    root = _frontend_root()
    try:
        await connect.handle_callback(installation_id, state)
    except CloudError:
        logger.warning("codeconnect.callback rejected", exc_info=True)
        return RedirectResponse(url=f"{root}/code?github=error", status_code=302)
    return RedirectResponse(url=f"{root}/code?github=connected", status_code=302)


@router.get("", response_model=CodeConnectionListResponse)
async def list_connections(
    ctx: RequestContext = Depends(request_context),
) -> CodeConnectionListResponse:
    """List the caller's GitHub connections (for the "connected as" UI state)."""
    workspace_id = _require_workspace(ctx)
    # connect.list_connections wraps the service read to lazily backfill each
    # connection's display info (account login + avatar) so the chip can render a
    # profile image without a reinstall.
    views = await connect.list_connections(workspace_id, ctx.user_id)
    return CodeConnectionListResponse(connections=[service.view_to_wire(v) for v in views])


@router.get("/repos", response_model=RepoListResponse)
async def list_repos(
    ctx: RequestContext = Depends(request_context),
) -> RepoListResponse:
    """List the repos the caller's connections can reach, for the repo picker."""
    workspace_id = _require_workspace(ctx)
    repos = await connect.list_repositories(workspace_id, ctx.user_id)
    return RepoListResponse(
        repos=[
            RepoResponse(
                fullName=r["full_name"],
                private=r["private"],
                defaultBranch=r["default_branch"],
                cloneUrl=r["clone_url"],
            )
            for r in repos
        ]
    )


__all__ = ["router"]
