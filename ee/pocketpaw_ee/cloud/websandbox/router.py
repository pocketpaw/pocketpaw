# router.py — Thin FastAPI adapter for the Web Cursor Sandbox Registry (WC-1).
# Created 2026-07-15 (feat/websandbox-registry): workspace+user scoped
# /api/v1/websandbox. Tenancy comes from the RequestContext (never the body or
# query), the service does the work, and CloudError -> JSON is handled by
# _core.http — this router never raises HTTPException.
#
# NOTE (WC-1 scope): endpoints are gated by license + an authenticated context
# only; per-action RBAC (require_action) is deferred to a later slice because it
# needs new action strings registered in the RBAC catalog. The tenancy/owner
# filtering in the service is the security boundary for now.
#
# Changed 2026-07-15 (WC-2, feat/websandbox-vm-provision): added the two
# cold-provision endpoints — ``POST /websandbox/open`` (provision a Daytona VM +
# clone a PUBLIC repo, returning the ready registry row) and
# ``GET /websandbox/{row_id}/tree`` (the cloned repo's file tree). Both delegate
# to ``websandbox/provision.py``; tenancy still comes only from the RequestContext.
#
# Changed 2026-07-15 (WC-S3, feat/websandbox-s3-durability): added the two
# workspace-durability endpoints — ``POST /websandbox/{row_id}/snapshot``
# (tar the VM workspace → land it in the tenant's S3 → return ``{fileId}``) and
# ``POST /websandbox/{row_id}/restore`` (fetch the snapshot from S3 → untar it
# into the VM, 204). Both delegate to ``websandbox/durability.py``; tenancy still
# comes only from the RequestContext, and both are license-gated like the rest.
#
# Changed 2026-07-15 (WC-5a, feat/websandbox-edit-agent): added the AI edit
# endpoint — ``POST /websandbox/{row_id}/edit`` (a file + instruction → a PROPOSED
# rewrite from a backend-side frontier model). Thin adapter over
# ``websandbox/edit.py``; tenancy comes only from the RequestContext, and it's
# license-gated like the rest. Generate-only — the frontend applies accepted hunks
# via the existing file-RPC.
#
# Changed 2026-07-16 (WC-7/P4a, feat/code-mode): added the git write-path routes —
# ``GET /websandbox/{row_id}/git/status`` and ``POST .../git/stage|commit|push``.
# Thin adapters over ``websandbox/git.py`` (git runs in the VM; the push token
# never enters it). Tenancy comes only from the RequestContext; a push failure is
# a ``pushed:false`` body, never a 500.
#
# Changed 2026-07-16 (WC-7/P4b, feat/code-mode): added ``POST .../git/pr`` — open a
# GitHub pull request for the pushed ``paw/edit-*`` branch via the GitHub App
# (server-side; the token never enters the VM). Thin adapter over
# ``websandbox/git.py:open_pr``; tenancy comes only from the RequestContext.
#
# Changed 2026-07-16 (WC-8/P3b, feat/code-mode): added the live-preview endpoint —
# ``GET /websandbox/{row_id}/preview?port=<int>`` returns the iframe-embeddable
# public URL of a dev-server port running in the VM. Thin adapter over
# ``websandbox/preview.py``; tenancy comes only from the RequestContext, the port
# is a validated query param (out-of-range / the reserved terminal port refused),
# and it's license-gated like the rest.
#
# Changed 2026-07-18 (BP-1b, feat/code-mode): added
# ``GET /websandbox/browserpod/credentials`` — issues the boot credential for the
# in-tab BrowserPod runtime. The key lives ONLY in server config (never in the
# frontend bundle); the route is license-gated and workspace-scoped like the rest,
# and an unconfigured deploy answers ``available:false`` so the client falls back
# to Daytona instead of erroring. Thin adapter over ``websandbox/browserpod.py``.
#
# Changed 2026-07-16 (review hardening): the register route now binds the
# repo-only ``RegisterSandboxRequest`` so a client can no longer write a
# server-owned ``sandbox_id`` / ``status`` (that field is the key
# ``authorize_sandbox`` trusts — a forgeable binding was a cross-tenant VM
# takeover). The client-facing ``PATCH /{row_id}`` lifecycle route was removed
# for the same reason: lifecycle is driven entirely by the provisioner + reaper,
# never the client.
from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.websandbox import archive as websandbox_archive
from pocketpaw_ee.cloud.websandbox import browserpod as websandbox_browserpod
from pocketpaw_ee.cloud.websandbox import durability as websandbox_durability
from pocketpaw_ee.cloud.websandbox import edit as websandbox_edit
from pocketpaw_ee.cloud.websandbox import git as websandbox_git
from pocketpaw_ee.cloud.websandbox import preview as websandbox_preview
from pocketpaw_ee.cloud.websandbox import provision as websandbox_provision
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.dto import (
    BrowserPodCredentialsResponse,
    CommitRequest,
    CreatePrRequest,
    CreateSandboxRequest,
    EditRequest,
    EditResponse,
    GitCommitResponse,
    GitPrResponse,
    GitPushResponse,
    GitStatusResponse,
    OpenSandboxRequest,
    PreviewResponse,
    RegisterSandboxRequest,
    SandboxTreeResponse,
    SnapshotResponse,
    StageRequest,
    WebSandboxListResponse,
    WebSandboxResponse,
)

router = APIRouter(
    prefix="/websandbox",
    tags=["WebSandbox"],
    dependencies=[Depends(require_license)],
)


def _require_workspace(ctx: RequestContext) -> str:
    """A workspace-scoped route needs an active workspace; fail closed if absent."""
    if not ctx.workspace_id:
        raise Forbidden("websandbox.no_workspace", "No active workspace")
    return ctx.workspace_id


@router.post("", response_model=WebSandboxResponse)
async def create_sandbox(
    body: RegisterSandboxRequest,
    ctx: RequestContext = Depends(request_context),
) -> WebSandboxResponse:
    workspace_id = _require_workspace(ctx)
    # Bind ONLY the repo from the client; sandbox_id/status stay server-owned
    # (the provisioner sets them). Constructing the internal command here keeps
    # the register row at status "pending" with no bound Daytona id.
    view = await websandbox_service.create_sandbox(
        workspace_id, ctx.user_id, CreateSandboxRequest(repo=body.repo)
    )
    return websandbox_service.view_to_wire(view)


@router.get("", response_model=WebSandboxListResponse)
async def list_sandboxes(
    ctx: RequestContext = Depends(request_context),
) -> WebSandboxListResponse:
    workspace_id = _require_workspace(ctx)
    views = await websandbox_service.list_sandboxes(workspace_id, ctx.user_id)
    return WebSandboxListResponse(items=[websandbox_service.view_to_wire(v) for v in views])


@router.get("/{row_id}", response_model=WebSandboxResponse)
async def get_sandbox(
    row_id: str,
    ctx: RequestContext = Depends(request_context),
) -> WebSandboxResponse:
    workspace_id = _require_workspace(ctx)
    view = await websandbox_service.get_sandbox(workspace_id, ctx.user_id, row_id)
    return websandbox_service.view_to_wire(view)


# NOTE: there is deliberately NO client ``PATCH /{row_id}`` route. A sandbox's
# lifecycle (status, bound Daytona id, feature branch) is server-owned and driven
# entirely by the provisioner (``provision.open_sandbox``) and the idle reaper —
# never the client. Exposing a client status-write let a caller bind an arbitrary
# ``sandbox_id`` onto its own row and forge access to another tenant's VM.


# ---------------------------------------------------------------------------
# WC-2 — cold-provision + file tree.
# ---------------------------------------------------------------------------


@router.post("/open", response_model=WebSandboxResponse)
async def open_sandbox(
    body: OpenSandboxRequest,
    ctx: RequestContext = Depends(request_context),
) -> WebSandboxResponse:
    """Cold-provision a Daytona VM and clone a PUBLIC repo into it."""
    workspace_id = _require_workspace(ctx)
    view = await websandbox_provision.open_sandbox(workspace_id, ctx.user_id, body)
    return websandbox_service.view_to_wire(view)


@router.get("/{row_id}/tree", response_model=SandboxTreeResponse)
async def get_sandbox_tree(
    row_id: str,
    ctx: RequestContext = Depends(request_context),
) -> SandboxTreeResponse:
    """Return the cloned repo's file tree for a ready sandbox."""
    workspace_id = _require_workspace(ctx)
    return await websandbox_provision.get_tree(workspace_id, ctx.user_id, row_id)


# ---------------------------------------------------------------------------
# WC-8/P3b — live dev-server preview.
# ---------------------------------------------------------------------------


@router.get("/{row_id}/preview", response_model=PreviewResponse)
async def get_sandbox_preview(
    row_id: str,
    port: int = Query(..., description="Dev-server port running in the sandbox VM"),
    ctx: RequestContext = Depends(request_context),
) -> PreviewResponse:
    """Return the iframe-embeddable public URL for a dev-server port in the VM."""
    workspace_id = _require_workspace(ctx)
    return await websandbox_preview.get_preview(workspace_id, ctx.user_id, row_id, port)


# ---------------------------------------------------------------------------
# BP-1b — BrowserPod boot credential. Two literal segments, so it can never be
# captured by the single-segment ``/{row_id}`` route above.
# ---------------------------------------------------------------------------


@router.get("/browserpod/repo-archive")
async def get_repo_archive(
    repo: str = Query(..., description="owner/repo or a github.com URL"),
    ref: str | None = Query(None, description="Branch, tag or commit (default branch if omitted)"),
    ctx: RequestContext = Depends(request_context),
) -> Response:
    """Serve a repo's source as a zip, for seeding an in-tab BrowserPod pod.

    The pod has no usable networking of its own for this: cloning inside it runs
    on BrowserPod's emulated TCP/TLS relay, and the browser cannot fetch GitHub's
    archives directly because they carry no CORS headers. Fetching server-side
    solves both, and is where a GitHub App token would live for private repos.

    Path note: TWO literal segments, deliberately. A single-segment path like
    ``/repo-archive`` is captured by the ``/{row_id}`` route above — FastAPI
    matches in registration order — so it resolved to "look up a sandbox called
    repo-archive" and 404'd. Any new collection-level route here needs the same
    treatment (see ``test_websandbox_routes.py``).
    """
    workspace_id = _require_workspace(ctx)
    content = await websandbox_archive.fetch_repo_archive(workspace_id, ctx.user_id, repo, ref)
    return Response(content=content, media_type="application/zip")


@router.get("/browserpod/credentials", response_model=BrowserPodCredentialsResponse)
async def get_browserpod_credentials(
    ctx: RequestContext = Depends(request_context),
) -> BrowserPodCredentialsResponse:
    """Issue the credential the browser needs to boot an in-tab BrowserPod pod.

    The key lives ONLY in server config; it is never built into the frontend
    bundle. License + an authenticated, workspace-scoped context gate the issue.
    An unconfigured deploy returns ``available: false`` so the client falls back
    to the Daytona runtime rather than erroring.
    """
    workspace_id = _require_workspace(ctx)
    return await websandbox_browserpod.get_credentials(workspace_id, ctx.user_id)


# ---------------------------------------------------------------------------
# WC-7/P4a — git write path (status / stage / commit / push).
# ---------------------------------------------------------------------------


@router.get("/{row_id}/git/status", response_model=GitStatusResponse)
async def git_status(
    row_id: str,
    ctx: RequestContext = Depends(request_context),
) -> GitStatusResponse:
    """Return the working-tree status (branch, ahead/behind, changed files)."""
    workspace_id = _require_workspace(ctx)
    return await websandbox_git.git_status(workspace_id, ctx.user_id, row_id)


@router.post("/{row_id}/git/stage", response_model=GitStatusResponse)
async def git_stage(
    row_id: str,
    body: StageRequest,
    ctx: RequestContext = Depends(request_context),
) -> GitStatusResponse:
    """Stage (or unstage) paths, then return a fresh status."""
    workspace_id = _require_workspace(ctx)
    return await websandbox_git.stage(workspace_id, ctx.user_id, row_id, body)


@router.post("/{row_id}/git/commit", response_model=GitCommitResponse)
async def git_commit(
    row_id: str,
    body: CommitRequest,
    ctx: RequestContext = Depends(request_context),
) -> GitCommitResponse:
    """Commit the staged changes as the caller's resolved identity."""
    workspace_id = _require_workspace(ctx)
    return await websandbox_git.commit(workspace_id, ctx.user_id, row_id, body)


@router.post("/{row_id}/git/push", response_model=GitPushResponse)
async def git_push(
    row_id: str,
    ctx: RequestContext = Depends(request_context),
) -> GitPushResponse:
    """Push the sandbox's feature branch to origin (never 500s on push failure)."""
    workspace_id = _require_workspace(ctx)
    return await websandbox_git.push(workspace_id, ctx.user_id, row_id)


@router.post("/{row_id}/git/pr", response_model=GitPrResponse)
async def git_open_pr(
    row_id: str,
    body: CreatePrRequest,
    ctx: RequestContext = Depends(request_context),
) -> GitPrResponse:
    """Open a GitHub pull request for the sandbox's pushed feature branch."""
    workspace_id = _require_workspace(ctx)
    return await websandbox_git.open_pr(workspace_id, ctx.user_id, row_id, body)


# ---------------------------------------------------------------------------
# WC-5a — AI edit agent (Cmd-K).
# ---------------------------------------------------------------------------


@router.post("/{row_id}/edit", response_model=EditResponse)
async def propose_edit(
    row_id: str,
    body: EditRequest,
    ctx: RequestContext = Depends(request_context),
) -> EditResponse:
    """Propose a model-authored rewrite of a file (generate-only, no VM write)."""
    workspace_id = _require_workspace(ctx)
    return await websandbox_edit.propose_edit(workspace_id, ctx.user_id, row_id, body)


# ---------------------------------------------------------------------------
# WC-S3 — workspace durability (snapshot / restore).
# ---------------------------------------------------------------------------


@router.post("/{row_id}/snapshot", response_model=SnapshotResponse)
async def snapshot_sandbox(
    row_id: str,
    ctx: RequestContext = Depends(request_context),
) -> SnapshotResponse:
    """Snapshot the sandbox's workspace to the tenant's blob storage."""
    workspace_id = _require_workspace(ctx)
    file_id = await websandbox_durability.snapshot_workspace(workspace_id, ctx.user_id, row_id)
    return SnapshotResponse(fileId=file_id)


@router.post("/{row_id}/restore", status_code=204)
async def restore_sandbox(
    row_id: str,
    ctx: RequestContext = Depends(request_context),
) -> Response:
    """Restore the sandbox's latest workspace snapshot from blob storage into the VM."""
    workspace_id = _require_workspace(ctx)
    await websandbox_durability.restore_workspace(workspace_id, ctx.user_id, row_id)
    return Response(status_code=204)
