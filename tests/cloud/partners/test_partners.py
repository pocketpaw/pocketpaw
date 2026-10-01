# tests/cloud/partners/test_partners.py — Paw Partners foundation (PH-1).
#
# Created 2026-10-01 (feat/partners-foundation). Locks the contract: client CRUD
# round-trip, tenant isolation (404 cross-tenant), 403 for non-partner / non-active
# workspaces, platform-operator-only admin switch, and per-workspace site billing.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud.billing import enforcement
from pocketpaw_ee.cloud.models.workspace import PartnerProfile
from pocketpaw_ee.cloud.models.workspace import Workspace as WorkspaceDoc
from pocketpaw_ee.cloud.partners import router as partners_router
from pocketpaw_ee.cloud.partners import service, service_admin
from pocketpaw_ee.guards.platform import check_platform_action
from pocketpaw_ee.guards.rbac import Forbidden as GuardForbidden
from pydantic import ValidationError as PydanticValidationError

pytestmark = pytest.mark.asyncio

PHONE = "+919876543210"


def _ctx(workspace_id: str | None) -> RequestContext:
    return RequestContext(
        user_id="u1",
        workspace_id=workspace_id,
        request_id="r1",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


async def _workspace(slug: str, status: str | None = None) -> WorkspaceDoc:
    ws = WorkspaceDoc(name=slug, slug=slug, owner="u1")
    if status:
        ws.partner = PartnerProfile(status=status, footer_name=f"{slug} Prints")
    await ws.insert()
    return ws


@pytest.fixture
def flags_off(monkeypatch):
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(billing_enforced=False, sites_billing_enforced=False),
    )


# ---------------------------------------------------------------- clients


async def test_client_crud_round_trip(mongo_db) -> None:
    ws = await _workspace("acme", "active")
    ctx = _ctx(str(ws.id))

    created = await service.create_client(
        ctx, body={"name": "Ravi Stores", "whatsapp": PHONE, "gstin": "29ABCDE1234F1Z5"}
    )
    assert created.workspace_id == str(ws.id)
    assert (await service.get_client(ctx, client_id=created.id)).name == "Ravi Stores"
    assert [c.id for c in await service.list_clients(ctx)] == [created.id]

    updated = await service.update_client(ctx, client_id=created.id, body={"notes": "pays cash"})
    assert updated.notes == "pays cash"
    assert updated.name == "Ravi Stores"  # PATCH leaves unsent fields alone

    await service.delete_client(ctx, client_id=created.id)
    assert await service.list_clients(ctx) == []
    with pytest.raises(NotFound):
        await service.get_client(ctx, client_id=created.id)


async def test_whatsapp_must_be_e164(mongo_db) -> None:
    ws = await _workspace("acme", "active")
    with pytest.raises(PydanticValidationError):
        await service.create_client(_ctx(str(ws.id)), body={"name": "X", "whatsapp": "98765"})


async def test_cross_tenant_client_is_404(mongo_db) -> None:
    a = await _workspace("a", "active")
    b = await _workspace("b", "active")
    client = await service.create_client(_ctx(str(a.id)), body={"name": "A", "whatsapp": PHONE})

    ctx_b = _ctx(str(b.id))
    for call in (
        service.get_client(ctx_b, client_id=client.id),
        service.update_client(ctx_b, client_id=client.id, body={"notes": "x"}),
        service.delete_client(ctx_b, client_id=client.id),
    ):
        with pytest.raises(NotFound) as exc:
            await call
        assert exc.value.status_code == 404
    assert await service.list_clients(ctx_b) == []


@pytest.mark.parametrize("status", [None, "applied", "suspended"])
async def test_client_calls_from_non_active_partner_are_403(mongo_db, status) -> None:
    ws = await _workspace(f"ws-{status}", status)
    ctx = _ctx(str(ws.id))
    with pytest.raises(Forbidden) as exc:
        await service.list_clients(ctx)
    assert exc.value.status_code == 403
    with pytest.raises(Forbidden):
        await service.create_client(ctx, body={"name": "X", "whatsapp": PHONE})


async def test_me_is_404_without_a_profile_and_returns_it_with_one(mongo_db) -> None:
    plain = await _workspace("plain")
    with pytest.raises(NotFound):
        await service.get_profile(_ctx(str(plain.id)))
    partner = await _workspace("p", "suspended")
    out = await service.get_profile(_ctx(str(partner.id)))
    assert out.status == "suspended"
    assert out.billing_country == "IN"
    assert await service.get_active_profile(_ctx(str(partner.id))) is None


# ---------------------------------------------------------------- admin


async def test_admin_route_is_guarded_by_the_operator_rung() -> None:
    routes = {r.path: r for r in partners_router.admin_router.routes}
    route = routes["/admin/partners/{workspace_id}"]
    actions = {getattr(d.call, "__platform_action__", None) for d in route.dependant.dependencies}
    assert "platform.partners.write" in actions

    for role in (None, "support"):
        with pytest.raises(GuardForbidden):
            check_platform_action("platform.partners.write", role)
    check_platform_action("platform.partners.write", "operator")


async def test_admin_sets_updates_and_clears_a_partner(mongo_db) -> None:
    ws = await _workspace("shop")
    wid = str(ws.id)
    out = await service_admin.set_partner_profile(
        workspace_id=wid,
        body={"status": "active", "footer_name": "Shop Prints", "billing_country": "in"},
        operator_id="op1",
    )
    assert out is not None and out.status == "active" and out.billing_country == "IN"
    assert await service.get_active_profile(_ctx(wid)) is not None
    joined = out.joined_at

    out = await service_admin.set_partner_profile(
        workspace_id=wid,
        body={"status": "suspended", "footer_name": "Shop Prints"},
        operator_id="op1",
    )
    assert out.status == "suspended"
    assert out.joined_at.replace(tzinfo=None) == joined.replace(tzinfo=None)

    assert (
        await service_admin.set_partner_profile(workspace_id=wid, body=None, operator_id="op1")
        is None
    )
    assert (await WorkspaceDoc.get(ws.id)).partner is None

    with pytest.raises(NotFound):
        await service_admin.set_partner_profile(
            workspace_id="000000000000000000000000", body=None, operator_id="op1"
        )


async def test_bad_billing_country_rejected(mongo_db) -> None:
    ws = await _workspace("shop")
    with pytest.raises(PydanticValidationError):
        await service_admin.set_partner_profile(
            workspace_id=str(ws.id),
            body={"status": "active", "footer_name": "x", "billing_country": "IND"},
            operator_id="op1",
        )


# ---------------------------------------------------------------- billing


async def test_sites_enforced_reads_the_partner_profile(flags_off) -> None:
    assert enforcement.sites_enforced() is False
    assert enforcement.sites_enforced(PartnerProfile(status="active", footer_name="x")) is True
    assert enforcement.sites_enforced(PartnerProfile(status="suspended", footer_name="x")) is False
    assert enforcement.sites_enforced(PartnerProfile(status="applied", footer_name="x")) is False


async def test_sites_enforced_for_is_per_workspace(mongo_db, flags_off) -> None:
    active = await _workspace("active", "active")
    suspended = await _workspace("susp", "suspended")
    plain = await _workspace("plain")
    assert await enforcement.sites_enforced_for(str(active.id)) is True
    assert await enforcement.sites_enforced_for(str(suspended.id)) is False
    assert await enforcement.sites_enforced_for(str(plain.id)) is False
    assert await enforcement.sites_enforced_for(None) is False


async def test_global_flag_still_enforces_without_a_read(monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(billing_enforced=False, sites_billing_enforced=True),
    )
    # No mongo_db fixture: a DB read here would raise, so True proves the short-circuit.
    assert await enforcement.sites_enforced_for("000000000000000000000000") is True
