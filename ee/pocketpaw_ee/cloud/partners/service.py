# ee/pocketpaw_ee/cloud/partners/service.py — Paw Partners tenant service.
#
# Created 2026-10-01 (feat/partners-foundation, PH-1). Reads the caller's own
# partner profile and does tenant-filtered CRUD on ``PartnerClient`` records.
# Client routes require an ACTIVE partner profile (``Forbidden`` otherwise).
# ``get_active_profile`` / ``get_client`` are consumed by later PH tasks by name.
# ``partner_profile_for_workspace`` is the billing seam's loader
# (``billing.enforcement.sites_enforced_for``).

from __future__ import annotations

from typing import Any

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import (
    PartnerClientCreated,
    PartnerClientDeleted,
    PartnerClientUpdated,
)
from pocketpaw_ee.cloud.models.partner_client import PartnerClient as _ClientDoc
from pocketpaw_ee.cloud.models.workspace import PartnerProfile
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from pocketpaw_ee.cloud.partners.domain import PartnerClient
from pocketpaw_ee.cloud.partners.dto import (
    PartnerClientCreateRequest,
    PartnerClientOut,
    PartnerClientUpdateRequest,
    PartnerProfileOut,
)


def _oid(value: str | None) -> PydanticObjectId | None:
    try:
        return PydanticObjectId(value)
    except Exception:
        return None


async def partner_profile_for_workspace(workspace_id: str | None) -> PartnerProfile | None:
    """The partner profile of ``workspace_id``, or None (not a partner / no such id)."""
    oid = _oid(workspace_id)
    if oid is None:
        return None
    ws = await _WorkspaceDoc.find_one({"_id": oid, "deleted_at": None})
    return ws.partner if ws is not None else None


async def get_active_profile(ctx: RequestContext) -> PartnerProfile | None:
    """The caller's workspace partner profile when it is ``active``, else None."""
    profile = await partner_profile_for_workspace(ctx.workspace_id)
    return profile if profile is not None and profile.status == "active" else None


async def get_profile(ctx: RequestContext) -> PartnerProfileOut:
    profile = await partner_profile_for_workspace(ctx.workspace_id)
    if profile is None:
        raise NotFound("partner_profile", ctx.workspace_id or "")
    return PartnerProfileOut.model_validate(profile, from_attributes=True)


async def _require_active(ctx: RequestContext) -> str:
    if await get_active_profile(ctx) is None:
        raise Forbidden("partner.not_active", "This workspace is not an active partner")
    return ctx.workspace_id  # type: ignore[return-value]  # active ⇒ workspace resolved


def _to_out(doc: _ClientDoc) -> PartnerClientOut:
    domain = PartnerClient(
        id=str(doc.id),
        workspace_id=doc.workspace,
        name=doc.name,
        whatsapp=doc.whatsapp,
        whatsapp_opt_in_at=doc.whatsapp_opt_in_at,
        gstin=doc.gstin,
        notes=doc.notes,
        created_at=doc.createdAt,
        updated_at=doc.updatedAt,
    )
    return PartnerClientOut.model_validate(domain, from_attributes=True)


async def _load(workspace_id: str, client_id: str) -> _ClientDoc:
    oid = _oid(client_id)
    doc = (
        None if oid is None else await _ClientDoc.find_one({"_id": oid, "workspace": workspace_id})
    )
    if doc is None:
        raise NotFound("partner_client", client_id)
    return doc


async def list_clients(ctx: RequestContext) -> list[PartnerClientOut]:
    workspace_id = await _require_active(ctx)
    # ponytail: unpaginated, newest first; add a cursor when a partner nears ~1k clients.
    docs = await _ClientDoc.find({"workspace": workspace_id}).sort("-_id").to_list()
    return [_to_out(d) for d in docs]


async def get_client(ctx: RequestContext, *, client_id: str) -> PartnerClientOut:
    workspace_id = await _require_active(ctx)
    return _to_out(await _load(workspace_id, client_id))


async def create_client(ctx: RequestContext, *, body: Any) -> PartnerClientOut:
    body = PartnerClientCreateRequest.model_validate(body)
    workspace_id = await _require_active(ctx)
    doc = _ClientDoc(workspace=workspace_id, **body.model_dump())
    await doc.insert()
    out = _to_out(doc)
    await emit(PartnerClientCreated(data={"workspace_id": workspace_id, "client_id": out.id}))
    return out


async def update_client(ctx: RequestContext, *, client_id: str, body: Any) -> PartnerClientOut:
    body = PartnerClientUpdateRequest.model_validate(body)
    workspace_id = await _require_active(ctx)
    doc = await _load(workspace_id, client_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        if field in ("name", "whatsapp", "notes") and value is None:
            continue  # required on the record; null means "leave it"
        setattr(doc, field, value)
    await doc.save()
    out = _to_out(doc)
    await emit(PartnerClientUpdated(data={"workspace_id": workspace_id, "client_id": out.id}))
    return out


async def delete_client(ctx: RequestContext, *, client_id: str) -> None:
    workspace_id = await _require_active(ctx)
    doc = await _load(workspace_id, client_id)
    await doc.delete()
    await emit(PartnerClientDeleted(data={"workspace_id": workspace_id, "client_id": client_id}))
