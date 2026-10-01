# ee/pocketpaw_ee/cloud/partners/router.py — thin HTTP layer for Paw Partners.
#
# Created 2026-10-01 (feat/partners-foundation, PH-1).
#   /partners/me, /partners/clients[/{client_id}]  — tenant routes
#   /admin/partners/{workspace_id}                 — platform-operator set / clear

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.partners import service, service_admin
from pocketpaw_ee.cloud.partners.dto import (
    PartnerClientCreateRequest,
    PartnerClientOut,
    PartnerClientUpdateRequest,
    PartnerProfileIn,
    PartnerProfileOut,
)

router = APIRouter(prefix="/partners", tags=["partners"])
admin_router = APIRouter(prefix="/admin/partners", tags=["partners-admin"])

Ctx = Annotated[RequestContext, Depends(request_context)]
Operator = Annotated[User, Depends(require_platform("platform.partners.write"))]


@router.get("/me", response_model=PartnerProfileOut)
async def get_me(ctx: Ctx) -> PartnerProfileOut:
    return await service.get_profile(ctx)


@router.get("/clients", response_model=list[PartnerClientOut])
async def list_clients(ctx: Ctx) -> list[PartnerClientOut]:
    return await service.list_clients(ctx)


@router.post("/clients", response_model=PartnerClientOut, status_code=201)
async def create_client(body: PartnerClientCreateRequest, ctx: Ctx) -> PartnerClientOut:
    return await service.create_client(ctx, body=body)


@router.patch("/clients/{client_id}", response_model=PartnerClientOut)
async def update_client(
    client_id: str, body: PartnerClientUpdateRequest, ctx: Ctx
) -> PartnerClientOut:
    return await service.update_client(ctx, client_id=client_id, body=body)


@router.delete("/clients/{client_id}", status_code=204)
async def delete_client(client_id: str, ctx: Ctx) -> Response:
    await service.delete_client(ctx, client_id=client_id)
    return Response(status_code=204)


@admin_router.put("/{workspace_id}", response_model=PartnerProfileOut | None)
async def set_partner(
    workspace_id: str, body: PartnerProfileIn | None, operator: Operator
) -> PartnerProfileOut | None:
    """Set the profile; send ``null`` (or use DELETE) to clear it."""
    return await service_admin.set_partner_profile(
        workspace_id=workspace_id, body=body, operator_id=str(operator.id)
    )


@admin_router.delete("/{workspace_id}", status_code=204)
async def clear_partner(workspace_id: str, operator: Operator) -> Response:
    await service_admin.set_partner_profile(
        workspace_id=workspace_id, body=None, operator_id=str(operator.id)
    )
    return Response(status_code=204)
