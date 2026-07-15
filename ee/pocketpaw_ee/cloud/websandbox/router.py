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
# Changed 2026-07-16 (review hardening): the register route now binds the
# repo-only ``RegisterSandboxRequest`` so a client can no longer write a
# server-owned ``sandbox_id`` / ``status`` (that field is the key
# ``authorize_sandbox`` trusts — a forgeable binding was a cross-tenant VM
# takeover). The client-facing ``PATCH /{row_id}`` lifecycle route was removed
# for the same reason: lifecycle is driven entirely by the provisioner + reaper,
# never the client.
from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.websandbox import durability as websandbox_durability
from pocketpaw_ee.cloud.websandbox import edit as websandbox_edit
from pocketpaw_ee.cloud.websandbox import provision as websandbox_provision
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.dto import (
    CreateSandboxRequest,
    EditRequest,
    EditResponse,
    OpenSandboxRequest,
    RegisterSandboxRequest,
    SandboxTreeResponse,
    SnapshotResponse,
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
