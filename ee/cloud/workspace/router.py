"""Workspace domain — FastAPI router.

Authorization is declared at the route level via ``require_action(...)``.
Service module functions take ``RequestContext`` and return domain
entities; the router maps to DTOs at the boundary.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import Response

from ee.cloud._core.context import RequestContext, request_context
from ee.cloud._core.deps import (
    current_user,
    require_action,
    require_membership,
)
from ee.cloud.license import require_license
from ee.cloud.models.user import User
from ee.cloud.workspace import service as workspace_service
from ee.cloud.workspace.dto import (
    CreateInviteRequest,
    CreateWorkspaceRequest,
    InviteOut,
    MemberOut,
    UpdateMemberRoleRequest,
    UpdateWorkspaceRequest,
    ValidateInviteOut,
    WorkspaceOut,
    invite_to_dto,
    invite_to_validate_dto,
    member_to_dto,
    workspace_to_dto,
)

router = APIRouter(
    prefix="/workspaces", tags=["Workspace"], dependencies=[Depends(require_license)]
)


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------


@router.post("", response_model=WorkspaceOut)
async def create_workspace(
    body: CreateWorkspaceRequest,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(current_user),  # legacy presence — drives the auth chain
) -> WorkspaceOut:
    ws = await workspace_service.create(ctx, body)
    return workspace_to_dto(ws)


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(current_user),
) -> list[WorkspaceOut]:
    items = await workspace_service.list_for_user(ctx)
    return [workspace_to_dto(ws) for ws in items]


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_membership),
) -> WorkspaceOut:
    ws = await workspace_service.get(ctx, workspace_id)
    return workspace_to_dto(ws)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: str,
    body: UpdateWorkspaceRequest,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("workspace.update")),
) -> WorkspaceOut:
    ws = await workspace_service.update(ctx, workspace_id, body)
    return workspace_to_dto(ws)


@router.delete("/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("workspace.delete")),
) -> Response:
    await workspace_service.delete(ctx, workspace_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/members", response_model=list[MemberOut])
async def list_members(
    workspace_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_membership),
) -> list[MemberOut]:
    items = await workspace_service.list_members(ctx, workspace_id)
    return [member_to_dto(m) for m in items]


@router.patch("/{workspace_id}/members/{user_id}")
async def update_member_role(
    workspace_id: str,
    user_id: str,
    body: UpdateMemberRoleRequest,
    user: User = Depends(require_action("workspace.member.role_change")),
) -> dict:
    await workspace_service.update_member_role(workspace_id, user_id, body.role, str(user.id))
    return {"ok": True}


@router.delete("/{workspace_id}/members/{user_id}", status_code=204)
async def remove_member(
    workspace_id: str,
    user_id: str,
    user: User = Depends(require_action("workspace.member.remove")),
) -> Response:
    await workspace_service.remove_member(workspace_id, user_id, str(user.id))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/invites", response_model=list[InviteOut])
async def list_invites(
    workspace_id: str,
    user: User = Depends(require_action("invite.create")),
) -> list[InviteOut]:
    items = await workspace_service.list_invites(workspace_id)
    return [invite_to_dto(i) for i in items]


@router.post("/{workspace_id}/invites", response_model=InviteOut)
async def create_invite(
    workspace_id: str,
    body: CreateInviteRequest,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("invite.create")),
) -> InviteOut:
    invite = await workspace_service.create_invite(ctx, workspace_id, body)
    return invite_to_dto(invite)


@router.get("/invites/{token}", response_model=ValidateInviteOut)
async def validate_invite(token: str) -> ValidateInviteOut:
    invite, ws_name = await workspace_service.validate_invite(token)
    return invite_to_validate_dto(invite, ws_name)


@router.post("/invites/{token}/accept")
async def accept_invite(
    token: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(current_user),
) -> dict:
    await workspace_service.accept_invite(ctx, token)
    return {"ok": True}


@router.delete("/{workspace_id}/invites/{invite_id}", status_code=204)
async def revoke_invite(
    workspace_id: str,
    invite_id: str,
    user: User = Depends(require_action("invite.revoke")),
) -> Response:
    await workspace_service.revoke_invite(workspace_id, invite_id)
    return Response(status_code=204)
