"""Workspace domain — FastAPI router.

Authorization is declared at the route level via ``require_action(...)``.
Service module functions take ``RequestContext`` and return domain
entities; the router maps to DTOs at the boundary.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import (
    current_user,
    require_action,
    require_membership,
)
from pocketpaw_ee.cloud._core.rate_limit import (
    consume_invite_create_tokens,
    rate_limit_invite_create,
    rate_limit_invite_resend,
)
from pocketpaw_ee.cloud.auth.core import current_optional_user
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.workspace import domains as domains_service
from pocketpaw_ee.cloud.workspace import service as workspace_service
from pocketpaw_ee.cloud.workspace.dto import (
    AddDomainRequest,
    BulkInviteRequest,
    BulkInviteResponse,
    BulkInviteSkip,
    CreateInviteRequest,
    CreateWorkspaceRequest,
    InviteOut,
    InvitePreviewResponse,
    MemberOut,
    UpdateDomainRequest,
    UpdateMemberRoleRequest,
    UpdateWorkspaceRequest,
    ValidateInviteOut,
    VerifiedDomainOut,
    WorkspaceDeletePreviewResponse,
    WorkspaceOut,
    invite_to_dto,
    invite_to_validate_dto,
    member_to_dto,
    verified_domain_to_dto,
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


@router.get(
    "/{workspace_id}/delete-preview",
    response_model=WorkspaceDeletePreviewResponse,
)
async def delete_preview(
    workspace_id: str,
    user: User = Depends(require_action("workspace.delete")),
) -> dict:
    """Blast-radius counts for the delete confirmation UI.

    Gated by the same ``workspace.delete`` action as the destructive route —
    seeing the preview implies you're the one who could pull the trigger.
    """
    return await workspace_service.get_delete_preview(workspace_id)


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
    _rl: None = Depends(rate_limit_invite_create),
) -> InviteOut:
    invite = await workspace_service.create_invite(ctx, workspace_id, body)
    return invite_to_dto(invite)


@router.post("/{workspace_id}/invites/bulk", response_model=BulkInviteResponse)
async def bulk_create_invites(
    workspace_id: str,
    body: BulkInviteRequest,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("invite.create")),
) -> BulkInviteResponse:
    # FastAPI's Depends can't see the request body, so the limiter has
    # to be consumed inside the handler with the actual batch size.
    # Counted BEFORE the service call so a rejected batch never touches
    # the DB.
    consume_invite_create_tokens(ctx.user_id, workspace_id, len(body.emails))
    result = await workspace_service.bulk_create_invites(ctx, workspace_id, body)
    return BulkInviteResponse(
        created=[invite_to_dto(inv) for inv in result["created"]],
        skipped=[BulkInviteSkip(**s) for s in result["skipped"]],
    )


@router.get("/invites/{token}/preview", response_model=InvitePreviewResponse)
async def preview_invite_route(
    token: str,
    viewer: User | None = Depends(current_optional_user),
) -> dict:
    viewer_id = str(viewer.id) if viewer is not None else None
    return await workspace_service.preview_invite(token, viewer_id)


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


@router.post("/invites/{token}/decline", status_code=204)
async def decline_invite_route(token: str) -> Response:
    """Invitee-side decline. Public — the invitee may not have an account."""
    await workspace_service.decline_invite(token)
    return Response(status_code=204)


@router.delete("/{workspace_id}/invites/{invite_id}", status_code=204)
async def revoke_invite(
    workspace_id: str,
    invite_id: str,
    user: User = Depends(require_action("invite.revoke")),
) -> Response:
    await workspace_service.revoke_invite(workspace_id, invite_id, str(user.id))
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Verified domains (Wave 3 Task 12)
# ---------------------------------------------------------------------------


@router.post("/{workspace_id}/domains", response_model=VerifiedDomainOut)
async def add_workspace_domain(
    workspace_id: str,
    body: AddDomainRequest,
    user: User = Depends(require_action("workspace.update")),
) -> VerifiedDomainOut:
    entry = await domains_service.add_domain(workspace_id, body.domain)
    return verified_domain_to_dto(entry)


@router.get("/{workspace_id}/domains", response_model=list[VerifiedDomainOut])
async def list_workspace_domains(
    workspace_id: str,
    user: User = Depends(require_action("workspace.update")),
) -> list[VerifiedDomainOut]:
    entries = await domains_service.list_domains(workspace_id)
    return [verified_domain_to_dto(e) for e in entries]


@router.post("/{workspace_id}/domains/{domain}/verify", response_model=VerifiedDomainOut)
async def verify_workspace_domain(
    workspace_id: str,
    domain: str,
    user: User = Depends(require_action("workspace.update")),
) -> VerifiedDomainOut:
    entry = await domains_service.verify_domain(workspace_id, domain)
    return verified_domain_to_dto(entry)


@router.patch("/{workspace_id}/domains/{domain}", response_model=VerifiedDomainOut)
async def update_workspace_domain(
    workspace_id: str,
    domain: str,
    body: UpdateDomainRequest,
    user: User = Depends(require_action("workspace.update")),
) -> VerifiedDomainOut:
    entry = await domains_service.set_auto_join(workspace_id, domain, body.auto_join)
    return verified_domain_to_dto(entry)


@router.delete("/{workspace_id}/domains/{domain}", status_code=204)
async def delete_workspace_domain(
    workspace_id: str,
    domain: str,
    user: User = Depends(require_action("workspace.update")),
) -> Response:
    await domains_service.remove_domain(workspace_id, domain)
    return Response(status_code=204)


@router.post("/{workspace_id}/invites/{invite_id}/resend")
async def resend_invite_route(
    workspace_id: str,
    invite_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("invite.resend")),
    _rl: None = Depends(rate_limit_invite_resend),
) -> dict:
    """Rotate the invite's token and return the fresh plaintext.

    The plaintext is the value the UI needs to put on the clipboard for
    the inviter — the server only stores the hash, so this is the only
    moment the plaintext exists outside the original email link.
    """
    return await workspace_service.resend_invite(ctx, workspace_id, invite_id)
