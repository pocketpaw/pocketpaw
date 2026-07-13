"""Workspace domain — FastAPI router.

Authorization is declared at the route level via ``require_action(...)``.
Service module functions take ``RequestContext`` and return domain
entities; the router maps to DTOs at the boundary.

2026-06-19 (feat/instinct-gate-integration, security-review FIX 1): added
``PATCH /{workspace_id}/instinct/approval-level`` — the OWNER-only
(``instinct.activate``) switch that activates the layered Instinct gate's
triager for a workspace. Kept off the general PATCH route because a non-ASK
level enables auto-approval of agent WRITE actions workspace-wide.
2026-07-10 (compliance-starter): added ``GET`` + ``PUT
/{workspace_id}/retention`` — read (member) / set (admin, ``workspace.update``)
the per-workspace data-retention policy. The PUT writes only ``retention_days``
without clobbering sibling settings.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
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
from pocketpaw_ee.cloud.pockets.dto import WorkspacePocketConnectorPermissionsOut
from pocketpaw_ee.cloud.workspace import domains as domains_service
from pocketpaw_ee.cloud.workspace import service as workspace_service
from pocketpaw_ee.cloud.workspace.dto import (
    AddDomainRequest,
    BulkInviteRequest,
    BulkInviteResponse,
    BulkInviteSkip,
    ConnectorPermissionsOut,
    CreateInviteRequest,
    CreateWorkspaceRequest,
    InviteOut,
    InvitePreviewResponse,
    MemberOut,
    RetentionOut,
    RoutePermissionsOut,
    SetApprovalLevelRequest,
    SetMemberConnectorPermissionsRequest,
    SetMemberRoutePermissionsRequest,
    SetRetentionRequest,
    SlugAvailabilityOut,
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


@router.get("/slug-available", response_model=SlugAvailabilityOut)
async def check_slug_available(
    slug: str = Query(..., min_length=1, max_length=50),
    user: User = Depends(current_user),  # signed-in users only reach create
) -> SlugAvailabilityOut:
    """Is this workspace slug free to claim?

    Declared before ``GET /{workspace_id}`` so the static path wins the
    route match. Returns ``available`` plus a ``reason`` (``invalid`` |
    ``reserved`` | ``taken``) so the create UI shows a precise message live
    instead of waiting for the POST to 409. The POST is still the
    source of truth — this is an advisory pre-check, racy by nature.
    """
    reason = await workspace_service.slug_reason(slug)
    return SlugAvailabilityOut(available=reason is None, reason=reason)


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


@router.patch("/{workspace_id}/instinct/approval-level", response_model=WorkspaceOut)
async def set_instinct_approval_level(
    workspace_id: str,
    body: SetApprovalLevelRequest,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("instinct.activate")),
) -> WorkspaceOut:
    """Activate (or stand down) the layered Instinct gate's triager for a
    workspace — security-review FIX 1.

    A non-ASK level turns ON auto-approval of agent WRITE actions for the whole
    workspace, so this is the most security-sensitive workspace write in the
    gate. It is a DEDICATED route — NOT folded into the general PATCH/
    ``UpdateWorkspaceRequest`` path — guarded by the OWNER-only
    ``instinct.activate`` action (the strongest workspace tier). The body is a
    closed enum (422 on anything else); the service re-validates against the
    ``ApprovalLevel`` enum and emits a WARNING audit event with the old→new
    level.
    """
    ws = await workspace_service.set_instinct_approval_level(ctx, workspace_id, body.level)
    return workspace_to_dto(ws)


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
# Data retention (compliance-starter)
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/retention", response_model=RetentionOut)
async def get_retention(
    workspace_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_membership),
) -> RetentionOut:
    """Read the workspace's data-retention policy.

    ``retention_days`` is ``None`` when the workspace keeps records forever.
    Any member may read the policy (it's config, not data); setting it is
    admin-gated on the PUT below.
    """
    days = await workspace_service.get_retention(ctx, workspace_id)
    return RetentionOut(retention_days=days)


@router.put("/{workspace_id}/retention", response_model=RetentionOut)
async def set_retention(
    workspace_id: str,
    body: SetRetentionRequest,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("workspace.update")),
) -> RetentionOut:
    """Set the workspace's data-retention policy (admin — ``workspace.update``).

    Writes ONLY ``settings.retention_days`` — sibling settings are preserved
    (the service merges rather than full-replaces). ``retention_days`` null =
    keep forever; a positive value is the age after which
    ``enforce_retention`` purges audit records. Emits a ``workspace.retention_set``
    audit row.
    """
    await workspace_service.set_retention(ctx, workspace_id, body.retention_days)
    return RetentionOut(retention_days=body.retention_days)


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
# Route Permissions
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/route-permissions", response_model=RoutePermissionsOut)
async def get_route_permissions(
    workspace_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_membership),
) -> RoutePermissionsOut:
    """Get the route-permissions map for the workspace.

    Returns a dict of user_id → list of allowed route keys. A missing or
    empty list means the user has full access (no restrictions).
    Admin/owner can see everyone's restrictions; members can only see their own.
    """
    result = await workspace_service.get_route_permissions(ctx, workspace_id)
    # Non-admin members can only see their own permissions
    viewer_id = str(user.id)
    members = await workspace_service.list_members(ctx, workspace_id)
    viewer_role = next(
        (m.role for m in members if m.user_id == viewer_id),
        "member",
    )
    if viewer_role not in ("owner", "admin"):
        # Filter to only the viewer's own permissions
        filtered = {k: v for k, v in result.items() if k == viewer_id}
        return RoutePermissionsOut(permissions=filtered)
    return RoutePermissionsOut(permissions=result)


@router.put("/{workspace_id}/route-permissions/{user_id}")
async def set_member_route_permissions(
    workspace_id: str,
    user_id: str,
    body: SetMemberRoutePermissionsRequest,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("workspace.member.role_change")),
) -> dict:
    """Set which routes a specific member can access.

    An empty ``routes`` list grants full access (clears all restrictions).
    Gated by the same ``workspace.member.role_change`` action as role changes.
    """
    await workspace_service.set_member_route_permissions(
        ctx,
        workspace_id,
        user_id,
        body.routes,
    )
    return {"ok": True}


@router.delete("/{workspace_id}/route-permissions/{user_id}", status_code=204)
async def clear_member_route_permissions(
    workspace_id: str,
    user_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("workspace.member.role_change")),
) -> Response:
    """Remove all route restrictions for a member (grants full access)."""
    await workspace_service.clear_member_route_permissions(ctx, workspace_id, user_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Connector Permissions
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/connector-permissions", response_model=ConnectorPermissionsOut)
async def get_connector_permissions(
    workspace_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_membership),
) -> ConnectorPermissionsOut:
    """Get the connector-permissions map for the workspace.

    Returns a dict of user_id → list of allowed connector names. A missing
    or empty list means the user has full access (no restrictions).
    Admin/owner can see everyone's restrictions; members can only see their own.
    """
    result = await workspace_service.get_connector_permissions(ctx, workspace_id)
    viewer_id = str(user.id)
    members = await workspace_service.list_members(ctx, workspace_id)
    viewer_role = next(
        (m.role for m in members if m.user_id == viewer_id),
        "member",
    )
    if viewer_role not in ("owner", "admin"):
        filtered = {k: v for k, v in result.items() if k == viewer_id}
        return ConnectorPermissionsOut(permissions=filtered)
    return ConnectorPermissionsOut(permissions=result)


@router.put("/{workspace_id}/connector-permissions/{user_id}")
async def set_member_connector_permissions(
    workspace_id: str,
    user_id: str,
    body: SetMemberConnectorPermissionsRequest,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("workspace.member.role_change")),
) -> dict:
    """Set which connectors a specific member can access.

    An empty ``connectors`` list grants full access (clears all restrictions).
    Gated by the same ``workspace.member.role_change`` action as role changes.
    """
    await workspace_service.set_member_connector_permissions(
        ctx,
        workspace_id,
        user_id,
        body.connectors,
    )
    return {"ok": True}


@router.delete("/{workspace_id}/connector-permissions/{user_id}", status_code=204)
async def clear_member_connector_permissions(
    workspace_id: str,
    user_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_action("workspace.member.role_change")),
) -> Response:
    """Remove all connector restrictions for a member (grants full access)."""
    await workspace_service.clear_member_connector_permissions(ctx, workspace_id, user_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Pocket Connector Permissions (workspace-level bulk read)
# ---------------------------------------------------------------------------


@router.get(
    "/{workspace_id}/pocket-connector-permissions",
    response_model=WorkspacePocketConnectorPermissionsOut,
)
async def get_workspace_pocket_connector_permissions(
    workspace_id: str,
    ctx: RequestContext = Depends(request_context),
    user: User = Depends(require_membership),
) -> WorkspacePocketConnectorPermissionsOut:
    """Read the per-pocket connector allowlist for every pocket in this workspace.

    Returns a map of ``pocket_id → allowed_connectors``. A ``null`` value
    means the pocket inherits all workspace connectors (default / no
    restrictions). An empty list means the pocket is restricted but has
    nothing allowed. Admin/owner sees every pocket's permissions; a regular
    member sees only pockets they can access (the service filters).
    """
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    result = await pockets_service.list_workspace_pocket_connector_permissions(
        workspace_id, user_id=str(user.id)
    )
    return WorkspacePocketConnectorPermissionsOut(permissions=result)


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
