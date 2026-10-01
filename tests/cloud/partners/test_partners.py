# tests/cloud/partners/test_partners.py — Paw Partners foundation (PH-1).
#
# Created 2026-10-01 (feat/partners-foundation). Locks the contract: client CRUD
# round-trip, tenant isolation (404 cross-tenant), 403 for non-partner / non-active
# workspaces, platform-operator-only admin switch, and per-workspace site billing.
# Updated the same day: clients are Fabric Customer objects; tests inject a
# journal-backed store over a tmp journal (same as tests/cloud/people).
# Review fixes: every client call passes the tmp store; an autouse fixture points
# SOUL_DATA_DIR at tmp_path and clears the default-store cache so nothing can
# reach a real journal; cross-tenant writes and reads share ONE store; admin PUT
# writes an audit row; a seam test proves partner billing at site_entitlements.

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
from soul_protocol.engine.journal import open_journal

from pocketpaw.fabric.journal_store import FabricJournalStore

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


@pytest.fixture(autouse=True)
def _no_real_journal(tmp_path, monkeypatch):
    """No test here may open the developer's real ~/.soul journal."""
    from pocketpaw.journal_dep import reset_journal_cache

    monkeypatch.setenv("SOUL_DATA_DIR", str(tmp_path / "soul"))
    service._default_store.cache_clear()
    reset_journal_cache()
    yield
    service._default_store.cache_clear()
    reset_journal_cache()


@pytest.fixture
def store(tmp_path):
    journal = open_journal(tmp_path / "journal.db")
    yield FabricJournalStore(journal)
    journal.close()


@pytest.fixture
def flags_off(monkeypatch):
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(billing_enforced=False, sites_billing_enforced=False),
    )


# ---------------------------------------------------------------- clients


async def test_client_crud_round_trip(mongo_db, store) -> None:
    ws = await _workspace("acme", "active")
    ctx = _ctx(str(ws.id))

    created = await service.create_client(
        ctx,
        body={"name": "Ravi Stores", "whatsapp": PHONE, "gstin": "29ABCDE1234F1Z5"},
        store=store,
    )
    assert created.workspace_id == str(ws.id)
    assert (await service.get_client(ctx, client_id=created.id, store=store)).name == "Ravi Stores"
    assert [c.id for c in await service.list_clients(ctx, store=store)] == [created.id]

    updated = await service.update_client(
        ctx, client_id=created.id, body={"notes": "pays cash"}, store=store
    )
    assert updated.notes == "pays cash"
    assert updated.name == "Ravi Stores"  # PATCH leaves unsent fields alone

    await service.delete_client(ctx, client_id=created.id, store=store)
    assert await service.list_clients(ctx, store=store) == []
    with pytest.raises(NotFound):
        await service.get_client(ctx, client_id=created.id, store=store)


async def test_whatsapp_must_be_e164(mongo_db, store) -> None:
    ws = await _workspace("acme", "active")
    with pytest.raises(PydanticValidationError):
        await service.create_client(
            _ctx(str(ws.id)), body={"name": "X", "whatsapp": "98765"}, store=store
        )


async def test_cross_tenant_client_is_404(mongo_db, store) -> None:
    a = await _workspace("a", "active")
    b = await _workspace("b", "active")
    ctx_a = _ctx(str(a.id))
    client = await service.create_client(ctx_a, body={"name": "A", "whatsapp": PHONE}, store=store)
    # Same store: A sees its client, so B's 404s below are tenancy, not an empty store.
    assert (await service.get_client(ctx_a, client_id=client.id, store=store)).name == "A"

    ctx_b = _ctx(str(b.id))
    for call in (
        service.get_client(ctx_b, client_id=client.id, store=store),
        service.update_client(ctx_b, client_id=client.id, body={"notes": "x"}, store=store),
        service.delete_client(ctx_b, client_id=client.id, store=store),
    ):
        with pytest.raises(NotFound) as exc:
            await call
        assert exc.value.status_code == 404
    assert await service.list_clients(ctx_b, store=store) == []
    # B's failed delete did not touch A's record.
    assert [c.id for c in await service.list_clients(ctx_a, store=store)] == [client.id]


@pytest.mark.parametrize("status", [None, "applied", "suspended"])
async def test_client_calls_from_non_active_partner_are_403(mongo_db, store, status) -> None:
    ws = await _workspace(f"ws-{status}", status)
    ctx = _ctx(str(ws.id))
    with pytest.raises(Forbidden) as exc:
        await service.list_clients(ctx, store=store)
    assert exc.value.status_code == 403
    with pytest.raises(Forbidden):
        await service.create_client(ctx, body={"name": "X", "whatsapp": PHONE}, store=store)


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


async def test_foreign_customer_objects_are_not_partner_clients(mongo_db, store) -> None:
    """Another journal writer's bare "customer" object never shows up as a client."""
    from pocketpaw.fabric.models import FabricObject

    ws = await _workspace("acme", "active")
    wid = str(ws.id)
    await store.create(
        FabricObject(id="crm-1", type_id="customer", properties={"name": "CRM"}),
        scope=[f"workspace:{wid}"],
    )
    ctx = _ctx(wid)
    assert await service.list_clients(ctx, store=store) == []
    with pytest.raises(NotFound):
        await service.get_client(ctx, client_id="crm-1", store=store)


async def test_opt_in_timestamp_round_trips(mongo_db, store) -> None:
    ws = await _workspace("acme", "active")
    ctx = _ctx(str(ws.id))
    at = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
    out = await service.create_client(
        ctx, body={"name": "R", "whatsapp": PHONE, "whatsapp_opt_in_at": at}, store=store
    )
    assert out.whatsapp_opt_in_at == at
    assert (await service.get_client(ctx, client_id=out.id, store=store)).whatsapp_opt_in_at == at


async def test_admin_put_writes_an_audit_row_and_null_clears(mongo_db) -> None:
    from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
    from pocketpaw_ee.cloud.models.user import User as UserDoc
    from pocketpaw_ee.cloud.partners.dto import PartnerProfileIn
    from starlette.datastructures import Headers
    from starlette.requests import Request

    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/v1/admin/partners/x",
            "headers": Headers(raw=[(b"user-agent", b"test")]).raw,
            "query_string": b"",
            "client": ("10.0.0.5", 1234),
        }
    )
    operator = UserDoc(email="op@paw.test", hashed_password="x", platform_role="operator")
    await operator.insert()
    ws = await _workspace("shop")
    wid = str(ws.id)

    body = PartnerProfileIn(status="active", footer_name="Shop Prints")
    out = await partners_router.set_partner(wid, body, request, operator)
    assert out is not None and out.status == "active"
    assert await partners_router.set_partner(wid, None, request, operator) is None
    assert (await WorkspaceDoc.get(ws.id)).partner is None

    rows = await PlatformAuditEvent.find_all().to_list()
    assert [r.action for r in rows] == ["platform.partners.write"] * 2
    assert all(r.target_workspace == wid and r.status == "applied" for r in rows)


async def test_partner_billing_reaches_the_site_entitlements_seam(mongo_db, monkeypatch) -> None:
    """Both global flags off: a free site in an ACTIVE partner workspace is refused the
    concierge at ``site_entitlements``; the same site in a plain workspace is not."""
    from pocketpaw_ee.cloud.billing import site_plans
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.sites import service as sites_service

    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(
            billing_enforced=False, sites_billing_enforced=False, dodo_site_products=None
        ),
    )

    async def free_site(ws: WorkspaceDoc) -> str:
        doc = Site(
            workspace=str(ws.id),
            pocket_id=f"pk_{ws.slug}",
            owner="u1",
            name="Shop",
            plan_tier=site_plans.BASE_SITE_PLAN_KEY,
            subscription_status="none",
            deployed=True,
        )
        await doc.insert()
        return str(doc.id)

    partner = await _workspace("partner", "active")
    plain = await _workspace("plain")
    p_ent = await sites_service.site_entitlements(
        workspace_id=str(partner.id), site_id=await free_site(partner)
    )
    n_ent = await sites_service.site_entitlements(
        workspace_id=str(plain.id), site_id=await free_site(plain)
    )
    assert p_ent.concierge_entitled is False  # enforced: the free floor sells no concierge
    assert n_ent.concierge_entitled is True  # unchanged: no billing, everything entitled
