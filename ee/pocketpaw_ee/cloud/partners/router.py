# ee/pocketpaw_ee/cloud/partners/router.py — thin HTTP layer for Paw Partners.
#
# Created 2026-10-01 (feat/partners-foundation, PH-1).
#   /partners/me, /partners/clients[/{client_id}]  — tenant routes, guarded with
#   ``require_action_any_workspace("fabric.read" | "fabric.write")`` exactly like
#   ``cloud/leads/router.py``. The operator switch is ``cloud/platform/partners.py``.

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from pocketpaw_ee.cloud._core.context import RequestContext, request_context
from pocketpaw_ee.cloud._core.deps import require_action_any_workspace
from pocketpaw_ee.cloud.partners import service
from pocketpaw_ee.cloud.partners.dto import (
    PartnerClientCreateRequest,
    PartnerClientOut,
    PartnerClientUpdateRequest,
    PartnerProfileOut,
)

router = APIRouter(prefix="/partners", tags=["partners"])

Ctx = Annotated[RequestContext, Depends(request_context)]
_READ = [Depends(require_action_any_workspace("fabric.read"))]
_WRITE = [Depends(require_action_any_workspace("fabric.write"))]


@router.get("/me", response_model=PartnerProfileOut, dependencies=_READ)
async def get_me(ctx: Ctx) -> PartnerProfileOut:
    return await service.get_profile(ctx)


@router.get("/clients", response_model=list[PartnerClientOut], dependencies=_READ)
async def list_clients(ctx: Ctx) -> list[PartnerClientOut]:
    return await service.list_clients(ctx)


@router.post("/clients", response_model=PartnerClientOut, status_code=201, dependencies=_WRITE)
async def create_client(body: PartnerClientCreateRequest, ctx: Ctx) -> PartnerClientOut:
    return await service.create_client(ctx, body=body)


@router.patch("/clients/{client_id}", response_model=PartnerClientOut, dependencies=_WRITE)
async def update_client(
    client_id: str, body: PartnerClientUpdateRequest, ctx: Ctx
) -> PartnerClientOut:
    return await service.update_client(ctx, client_id=client_id, body=body)


@router.delete("/clients/{client_id}", status_code=204, dependencies=_WRITE)
async def delete_client(client_id: str, ctx: Ctx) -> Response:
    """Delete = Fabric ARCHIVE: the client leaves every read, but the org journal
    keeps the full history, including the WhatsApp number and GSTIN. No erasure."""
    await service.delete_client(ctx, client_id=client_id)
    return Response(status_code=204)
