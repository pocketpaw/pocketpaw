"""Cross-tenant tenant directory — the operator's search box.

Created: 2026-09-14 (feat/platform-tenant-directory) — chunk 2 of the Paw Admin PRD.

Read-only. Nothing here mutates a tenant; writes arrive in chunks 5-7 and carry
audit records of their own.

Every route takes its target workspace as an explicit PATH parameter, which is
the exact inversion of the rule that governs the rest of this codebase
("workspace comes from the session, never from the caller"). That is legitimate
here and illegitimate everywhere else, which is why it lives behind the
``/platform`` prefix and ``require_platform``.

These routes read at the SUPPORT rung. A support operator is meant to be able
to answer "what is going on with this account" without being able to change
anything about it — that read/write split is the reason the axis has two rungs
rather than one.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.platform import audit
from pocketpaw_ee.cloud.workspace import service as workspace_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["platform"])


class WorkspaceRowOut(BaseModel):
    """One row of the tenant directory."""

    id: str
    name: str
    slug: str
    owner: str
    plan: str
    seats: int
    member_count: int
    created_at: datetime | None
    # Present and non-null means soft-deleted. Surfaced rather than filtered so
    # the console can show the row greyed out instead of leaving an operator to
    # conclude the account never existed.
    deleted_at: datetime | None


class WorkspacePageOut(BaseModel):
    items: list[WorkspaceRowOut]
    # Opaque. Pass back as ``cursor`` for the next page; null means this is the
    # last one. Deliberately not an offset or a total — see the service.
    next_cursor: str | None


class MemberOut(BaseModel):
    user_id: str
    email: str
    name: str
    role: str
    joined_at: datetime


class WorkspaceDetailOut(BaseModel):
    workspace: WorkspaceRowOut
    members: list[MemberOut]
    # Counted from memberships, not from the ``owner`` field, which can lag
    # behind reality after an ownership transfer.
    owner_count: int


def _row(workspace) -> WorkspaceRowOut:
    return WorkspaceRowOut(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        owner=workspace.owner,
        plan=workspace.plan,
        seats=workspace.seats,
        member_count=workspace.member_count,
        created_at=workspace.created_at,
        deleted_at=workspace.deleted_at,
    )


@router.get("", response_model=WorkspacePageOut)
async def search_workspaces(
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.workspace.read"))],
    q: Annotated[
        str | None,
        Query(
            min_length=2,
            description="Match slug, name, or the owner's email",
        ),
    ] = None,
    plan: Annotated[str | None, Query(description="Exact plan filter")] = None,
    include_deleted: Annotated[bool, Query(description="Include soft-deleted tenants")] = False,
    cursor: Annotated[str | None, Query(description="Opaque cursor from a previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> WorkspacePageOut:
    """Search every workspace on the deployment.

    ``q`` matches the three things a support request actually arrives with: a
    slug, a display name, or the owner's email address.
    """
    # No try/except here on purpose. A malformed cursor raises ValidationError,
    # which the central `cloud_error_handler` maps to the standard envelope —
    # the convention every router in this codebase follows ("never
    # HTTPException"). The earlier version caught Exception and matched on the
    # word "cursor" in the message, which was both fragile and wrong: it turned
    # a bad cursor into a 500.
    workspaces, next_cursor = await workspace_service.platform_search_workspaces(
        q=q,
        plan=plan,
        include_deleted=include_deleted,
        limit=limit,
        cursor=cursor,
    )

    # Audited. Enumerating tenants is the read an abused operator account makes
    # first, and request_logs stores the route template rather than the query
    # string — so without this row, WHAT was searched for is recorded nowhere.
    await audit.record_read(
        operator=operator,
        action="platform.workspace.read",
        query=f"q={q!r} plan={plan!r} include_deleted={include_deleted}",
        target_type="workspace_search",
        request=request,
    )

    return WorkspacePageOut(
        items=[_row(w) for w in workspaces],
        next_cursor=next_cursor,
    )


@router.get("/{workspace_id}", response_model=WorkspaceDetailOut)
async def get_workspace(
    workspace_id: str,
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.workspace.read"))],
) -> WorkspaceDetailOut:
    """One tenant, with its members.

    Soft-deleted tenants ARE returned here. An operator opening this page is
    usually asking why an account is gone, and answering that from the console
    is the point of the whole PRD.
    """
    # NotFound propagates to the central handler, same as everywhere else.
    workspace = await workspace_service.platform_get_workspace(workspace_id)

    members = await workspace_service.platform_list_members(workspace_id)

    await audit.record_read(
        operator=operator,
        action="platform.workspace.read",
        query=f"workspace={workspace_id}",
        target_type="workspace",
        target_workspace=workspace_id,
        request=request,
    )

    return WorkspaceDetailOut(
        workspace=_row(workspace),
        members=[
            MemberOut(
                user_id=m.user_id,
                email=m.email,
                name=m.name,
                role=m.role,
                joined_at=m.joined_at,
            )
            for m in members
        ],
        owner_count=sum(1 for m in members if m.role == "owner"),
    )


@router.get("/{workspace_id}/members", response_model=list[MemberOut])
async def list_members(
    workspace_id: str,
    request: Request,
    operator: Annotated[User, Depends(require_platform("platform.member.read"))],
) -> list[MemberOut]:
    """Members of one tenant.

    Separate from the detail route and gated on ``platform.member.read`` rather
    than ``platform.workspace.read``, so the two can diverge later without the
    member list quietly riding a broader grant.
    """
    members = await workspace_service.platform_list_members(workspace_id)

    await audit.record_read(
        operator=operator,
        action="platform.member.read",
        query=f"workspace={workspace_id}",
        target_type="workspace_members",
        target_workspace=workspace_id,
        request=request,
    )

    return [
        MemberOut(
            user_id=m.user_id,
            email=m.email,
            name=m.name,
            role=m.role,
            joined_at=m.joined_at,
        )
        for m in members
    ]
