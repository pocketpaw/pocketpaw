# ee/pocketpaw_ee/cloud/partners/service.py — Paw Partners tenant service.
#
# Created 2026-10-01 (feat/partners-foundation, PH-1). Reads the caller's own
# partner profile and does tenant-scoped CRUD on clients. Client routes require
# an ACTIVE partner profile (``Forbidden`` otherwise).
# Updated the same day: clients are Fabric ``Customer`` objects in the org
# journal (``FabricJournalStore``), scope ``workspace:<id>`` = tenancy, copying
# ``people/service.py``. The journal's ``fabric.object.*`` events ARE the
# emit-on-write, so no cloud realtime event is fired. Delete = Fabric archive.
# ``get_active_profile`` / ``get_client`` are consumed by later PH tasks by name.
# ``partner_profile_for_workspace`` is the billing seam's loader
# (``billing.enforcement.sites_enforced_for``).

from __future__ import annotations

from functools import lru_cache
from typing import Any
from uuid import uuid4

from beanie import PydanticObjectId
from soul_protocol.spec.journal import Actor

from pocketpaw.fabric.journal_store import FabricJournalStore
from pocketpaw.fabric.models import FabricObject, FabricQuery
from pocketpaw_ee.cloud._core.context import RequestContext
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud.models.workspace import PartnerProfile
from pocketpaw_ee.cloud.models.workspace import Workspace as _WorkspaceDoc
from pocketpaw_ee.cloud.partners.domain import (
    CUSTOMER_TYPE_ID,
    CUSTOMER_TYPE_NAME,
    SOURCE_PAW_PARTNERS,
    PartnerClient,
)
from pocketpaw_ee.cloud.partners.dto import (
    PartnerClientCreateRequest,
    PartnerClientOut,
    PartnerClientUpdateRequest,
    PartnerProfileOut,
)


@lru_cache(maxsize=1)
def _default_store() -> FabricJournalStore:
    """Process-wide journal-backed store (same as ``people.service``). Tests pass ``store=``."""
    from pocketpaw.journal_dep import get_journal

    store = FabricJournalStore(get_journal())
    store.bootstrap()
    return store


def _scope(workspace_id: str) -> list[str]:
    return [f"workspace:{workspace_id}"]


def _actor(ctx: RequestContext, scope: list[str]) -> Actor:
    return Actor(kind="user", id=f"user:{ctx.user_id}", scope_context=list(scope))


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


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _client_from_object(obj: FabricObject, *, workspace_id: str) -> PartnerClient:
    p = obj.properties
    return PartnerClient(
        id=obj.id,
        workspace_id=workspace_id,
        name=str(p.get("name", "")),
        whatsapp=str(p.get("whatsapp", "")),
        whatsapp_opt_in_at=p.get("whatsapp_opt_in_at") or None,
        gstin=p.get("gstin") or None,
        notes=str(p.get("notes", "")),
        created_at=obj.created_at,
        updated_at=obj.updated_at,
        source=str(p.get("source", SOURCE_PAW_PARTNERS)),
    )


def _to_out(client: PartnerClient) -> PartnerClientOut:
    return PartnerClientOut(
        id=client.id,
        workspace_id=client.workspace_id,
        name=client.name,
        whatsapp=client.whatsapp,
        whatsapp_opt_in_at=client.whatsapp_opt_in_at,
        gstin=client.gstin,
        notes=client.notes,
        created_at=client.created_at,
        updated_at=client.updated_at,
    )


def _ours(obj: FabricObject | None) -> bool:
    return (
        obj is not None
        and obj.type_id == CUSTOMER_TYPE_ID
        and obj.source_connector == SOURCE_PAW_PARTNERS
    )


async def _load(fabric: FabricJournalStore, workspace_id: str, client_id: str) -> FabricObject:
    # Scope-filtered: another workspace's id is indistinguishable from not-found.
    obj = await fabric.get(client_id, requester_scopes=_scope(workspace_id))
    if not _ours(obj):
        raise NotFound("partner_client", client_id)
    return obj  # type: ignore[return-value]


async def list_clients(
    ctx: RequestContext, *, store: FabricJournalStore | None = None
) -> list[PartnerClientOut]:
    workspace_id = await _require_active(ctx)
    fabric = store or _default_store()
    # ponytail: in-memory projection scan, unpaginated; add a cursor near ~1k clients.
    result = await fabric.query(
        FabricQuery(type_id=CUSTOMER_TYPE_ID, limit=10_000),
        requester_scopes=_scope(workspace_id),
    )
    objs = sorted((o for o in result.objects if _ours(o)), key=lambda o: o.created_at, reverse=True)
    return [_to_out(_client_from_object(o, workspace_id=workspace_id)) for o in objs]


async def get_client(
    ctx: RequestContext, *, client_id: str, store: FabricJournalStore | None = None
) -> PartnerClientOut:
    workspace_id = await _require_active(ctx)
    obj = await _load(store or _default_store(), workspace_id, client_id)
    return _to_out(_client_from_object(obj, workspace_id=workspace_id))


async def create_client(
    ctx: RequestContext, *, body: Any, store: FabricJournalStore | None = None
) -> PartnerClientOut:
    body = PartnerClientCreateRequest.model_validate(body)
    workspace_id = await _require_active(ctx)
    fabric = store or _default_store()
    scope = _scope(workspace_id)
    client_id = f"customer-{uuid4().hex}"  # not deterministic: two clients may share a name
    props = PartnerClient(
        id=client_id,
        workspace_id=workspace_id,
        name=body.name,
        whatsapp=body.whatsapp,
        whatsapp_opt_in_at=_iso(body.whatsapp_opt_in_at),
        gstin=body.gstin,
        notes=body.notes,
        created_at=None,
        updated_at=None,
    ).to_properties()
    obj = await fabric.create(
        FabricObject(
            id=client_id,
            type_id=CUSTOMER_TYPE_ID,
            type_name=CUSTOMER_TYPE_NAME,
            properties=props,
            source_connector=SOURCE_PAW_PARTNERS,
            source_id=workspace_id,
        ),
        scope=scope,
        actor=_actor(ctx, scope),
    )
    # no-event: the journal's fabric.object.created event is the emit-on-write.
    return _to_out(_client_from_object(obj, workspace_id=workspace_id))


async def update_client(
    ctx: RequestContext, *, client_id: str, body: Any, store: FabricJournalStore | None = None
) -> PartnerClientOut:
    body = PartnerClientUpdateRequest.model_validate(body)
    workspace_id = await _require_active(ctx)
    fabric = store or _default_store()
    await _load(fabric, workspace_id, client_id)
    changes: dict[str, Any] = {}
    for field, value in body.model_dump(exclude_unset=True).items():
        if field in ("name", "whatsapp", "notes") and value is None:
            continue  # required on the record; null means "leave it"
        changes[field] = _iso(value) if field == "whatsapp_opt_in_at" else value
    scope = _scope(workspace_id)
    obj = await fabric.update(client_id, changes, scope=scope, actor=_actor(ctx, scope))
    if obj is None:
        raise NotFound("partner_client", client_id)
    # no-event: the journal's fabric.object.updated event is the emit-on-write.
    return _to_out(_client_from_object(obj, workspace_id=workspace_id))


async def delete_client(
    ctx: RequestContext, *, client_id: str, store: FabricJournalStore | None = None
) -> None:
    workspace_id = await _require_active(ctx)
    fabric = store or _default_store()
    await _load(fabric, workspace_id, client_id)
    scope = _scope(workspace_id)
    await fabric.archive(
        client_id, scope=scope, reason="deleted by partner", actor=_actor(ctx, scope)
    )
    # no-event: the journal's fabric.object.archived event is the emit-on-write.
