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
# Updated 2026-10-02 (quality review): HTTP-level tests for the operator switch
# (now /platform/workspaces/{id}/partner) and the client-route action guards,
# a seam that REFUSES (badge removal), empty-PATCH no-op and GSTIN validation.
# Updated 2026-10-02 (feat/partners-sell, PH-2): offers per country, selling a
# yearly plan through the publish path (exact debit, redeploy, idempotent, short
# wallet changes nothing, partner/client/site guards), the renewal sweep on a
# yearly rung, partner-tier entitlements, the per-year quota window, and the
# sold-sites list with and without ``due_within_days``.
# Review fix (B1): period changes — the two reproduced leaks (year -> month ->
# year restarting a year free; year -> cheaper monthly staff for nothing) now
# fail closed; a lapsed year moves as a fresh purchase; a renewal with the
# partner profile gone keeps the price last paid, or refuses.
# Updated 2026-10-02: the autouse fixture clears the shared
# ``read_model.default_journal_store`` cache (``service._default_store`` now
# delegates to it) and points the per-workspace stores at tmp_path.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from dateutil.relativedelta import relativedelta
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud.billing import enforcement
from pocketpaw_ee.cloud.models.workspace import PartnerProfile
from pocketpaw_ee.cloud.models.workspace import Workspace as WorkspaceDoc
from pocketpaw_ee.cloud.partners import service
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
    """No test here may open the developer's real ~/.soul journal or ~/.pocketpaw stores."""
    from pocketpaw import stores
    from pocketpaw.fabric import read_model
    from pocketpaw.journal_dep import reset_journal_cache

    monkeypatch.setenv("SOUL_DATA_DIR", str(tmp_path / "soul"))
    monkeypatch.setattr(stores, "_DATA_DIR", tmp_path / "pocketpaw")
    stores.reset_store_caches()
    read_model.default_journal_store.cache_clear()
    reset_journal_cache()
    yield
    read_model.default_journal_store.cache_clear()
    stores.reset_store_caches()
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


async def test_empty_patch_writes_nothing(mongo_db, store, monkeypatch) -> None:
    ws = await _workspace("acme", "active")
    ctx = _ctx(str(ws.id))
    out = await service.create_client(ctx, body={"name": "R", "whatsapp": PHONE}, store=store)

    async def _no_write(*_a: Any, **_k: Any) -> None:
        raise AssertionError("an empty PATCH must not write")

    monkeypatch.setattr(store, "update", _no_write)
    assert await service.update_client(ctx, client_id=out.id, body={}, store=store) == out


async def test_gstin_is_upper_cased_and_pattern_checked(mongo_db, store) -> None:
    ws = await _workspace("acme", "active")
    ctx = _ctx(str(ws.id))
    out = await service.create_client(
        ctx, body={"name": "R", "whatsapp": PHONE, "gstin": " 29abcde1234f1z5 "}, store=store
    )
    assert out.gstin == "29ABCDE1234F1Z5"
    with pytest.raises(PydanticValidationError):
        await service.create_client(
            ctx, body={"name": "R", "whatsapp": PHONE, "gstin": "29ABCDE1234F1Z"}, store=store
        )


async def test_partner_billing_refuses_badge_removal_at_the_seam(mongo_db, monkeypatch) -> None:
    """Flags off: a free site in an ACTIVE partner workspace is REFUSED badge removal
    by ``update_site_branding``; the same request in a plain workspace goes through."""
    from pocketpaw_ee.cloud._core.errors import BadgeRemovalNotEntitled
    from pocketpaw_ee.cloud.billing import site_plans
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.sites import service as sites_service
    from pocketpaw_ee.sites.dto import SiteBrandingUpdate

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

    hide = SiteBrandingUpdate(badge_hidden=True)
    partner = await _workspace("partner", "active")
    with pytest.raises(BadgeRemovalNotEntitled):
        await sites_service.update_site_branding(
            workspace_id=str(partner.id), site_id=await free_site(partner), body=hide
        )
    plain = await _workspace("plain")
    out = await sites_service.update_site_branding(
        workspace_id=str(plain.id), site_id=await free_site(plain), body=hide
    )
    assert out.badge_hidden is True


# ---------------------------------------------------------------- HTTP: operator switch


async def _operator(role: str) -> Any:
    from pocketpaw_ee.cloud.models.user import User as UserDoc

    user = UserDoc(email=f"{role}@paw.test", hashed_password="x", platform_role=role)
    await user.insert()
    return user


@pytest_asyncio.fixture
async def platform_http(mongo_db):
    """The real platform router, guard and error handler; only the session user is faked."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.platform.router import router as platform_router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(platform_router, prefix="/api/v1")
    holder: dict[str, Any] = {}
    app.dependency_overrides[current_active_user] = lambda: holder["user"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t", cookies={"paw_auth": "session"}
    ) as c:
        yield c, holder


def _url(wid: str) -> str:
    return f"/api/v1/platform/workspaces/{wid}/partner"


async def test_http_operator_sets_then_clears_a_partner_with_audit(platform_http) -> None:
    from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent

    client, holder = platform_http
    holder["user"] = await _operator("operator")
    ws = await _workspace("shop")
    wid = str(ws.id)

    r = await client.put(
        _url(wid),
        json={
            "status": "active",
            "footer_name": "Shop Prints",
            "billing_country": "in",
            "reason": "signed",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["partner"]["status"] == "active"
    assert r.json()["partner"]["billing_country"] == "IN"
    joined = r.json()["partner"]["joined_at"]

    r = await client.put(
        _url(wid), json={"status": "suspended", "footer_name": "x", "reason": "late"}
    )
    assert r.status_code == 200 and r.json()["partner"]["joined_at"] == joined

    r = await client.request("DELETE", _url(wid), json={"reason": "left the program"})
    assert r.status_code == 200 and r.json()["partner"] is None
    assert (await WorkspaceDoc.get(ws.id)).partner is None

    rows = await PlatformAuditEvent.find_all().to_list()
    assert [r.action for r in rows] == ["platform.partners.write"] * 3
    assert all(r.target_workspace == wid and r.status == "applied" for r in rows)


async def test_http_put_requires_a_body_and_a_reason(platform_http) -> None:
    client, holder = platform_http
    holder["user"] = await _operator("operator")
    ws = await _workspace("shop", "active")
    wid = str(ws.id)

    assert (await client.put(_url(wid))).status_code == 422  # missing body never clears
    bad = {"status": "active", "footer_name": "x", "billing_country": "IND", "reason": "r"}
    assert (await client.put(_url(wid), json=bad)).status_code == 422
    no_reason = {"status": "active", "footer_name": "x", "reason": "  "}
    assert (await client.put(_url(wid), json=no_reason)).status_code == 422
    assert (await client.request("DELETE", _url(wid), json={"reason": ""})).status_code == 422
    assert (await WorkspaceDoc.get(ws.id)).partner.status == "active"  # nothing changed


async def test_http_guard_refuses_support_and_bearer(platform_http) -> None:
    client, holder = platform_http
    ws = await _workspace("shop")
    body = {"status": "active", "footer_name": "x", "reason": "r"}

    holder["user"] = await _operator("support")
    assert (await client.put(_url(str(ws.id)), json=body)).status_code == 403

    holder["user"] = await _operator("operator")
    r = await client.put(_url(str(ws.id)), json=body, headers={"Authorization": "Bearer t"})
    assert r.status_code == 403
    assert (await WorkspaceDoc.get(ws.id)).partner is None


async def test_http_missing_workspace_is_404(platform_http) -> None:
    client, holder = platform_http
    holder["user"] = await _operator("operator")
    body = {"status": "active", "footer_name": "x", "reason": "r"}
    assert (await client.put(_url("000000000000000000000000"), json=body)).status_code == 404


# ---------------------------------------------------------------- HTTP: client route guards


@pytest_asyncio.fixture
async def partners_http(mongo_db, store, monkeypatch):
    """The real partners router with its action guards; user and context are faked."""
    from pocketpaw_ee.cloud._core.context import request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.partners.router import router

    ws = await _workspace("acme", "active")
    wid = str(ws.id)
    holder: dict[str, Any] = {}
    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_active_user] = lambda: holder["user"]
    app.dependency_overrides[current_workspace_id] = lambda: wid
    app.dependency_overrides[request_context] = lambda: _ctx(wid)
    monkeypatch.setattr(service, "_default_store", lambda: store)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, holder, wid


def _user(workspace_id: str, role: str | None) -> SimpleNamespace:
    memberships = [SimpleNamespace(workspace=workspace_id, role=role)] if role else []
    return SimpleNamespace(id="u1", active_workspace=workspace_id, workspaces=memberships)


async def test_http_member_can_write_and_read_clients(partners_http) -> None:
    client, holder, wid = partners_http
    holder["user"] = _user(wid, "member")
    r = await client.post("/api/v1/partners/clients", json={"name": "R", "whatsapp": PHONE})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    assert (await client.patch(f"/api/v1/partners/clients/{cid}", json={})).status_code == 200
    assert [c["id"] for c in (await client.get("/api/v1/partners/clients")).json()] == [cid]
    assert (await client.delete(f"/api/v1/partners/clients/{cid}")).status_code == 204


async def test_http_non_member_cannot_read_or_write_clients(partners_http) -> None:
    client, holder, wid = partners_http
    holder["user"] = _user(wid, None)
    r = await client.post("/api/v1/partners/clients", json={"name": "R", "whatsapp": PHONE})
    assert r.status_code == 403
    assert (await client.get("/api/v1/partners/clients")).status_code == 403
    assert (await client.get("/api/v1/partners/me")).status_code == 403


# ---------------------------------------------------------------- selling (PH-2)
#
# A sale runs the ordinary paid-publish path (``publish_pocket``), so these use the
# same local-deploy seam as tests/cloud/sites/test_credits_publish.py. Partner
# workspaces are on ``go`` (the sites feature) with the plan's site slot left
# EMPTY on purpose: a partner-only tier must never be plan-carried, and an open
# slot is what would carry it.


def _sell_seams(monkeypatch) -> list[str]:
    """Local deploy + a bundle reader that needs no build, injected through the
    module attribute ``sell_site_plan`` calls."""
    from pocketpaw_ee.sites import service as sites_service

    from tests.cloud.sites.test_credits_publish import _local_deploy

    deploys = _local_deploy(monkeypatch)
    real = sites_service.publish_pocket

    async def _publish(**kw):
        return await real(**kw, _bundle_reader=lambda d: b"x")

    monkeypatch.setattr(sites_service, "publish_pocket", _publish)
    return deploys


async def _partner_ws(slug: str, *, country: str = "IN", status: str = "active") -> str:
    ws = WorkspaceDoc(name=slug, slug=slug, owner="u1", plan="go")
    ws.partner = PartnerProfile(
        status=status, footer_name=f"{slug} Prints", billing_country=country
    )
    await ws.insert()
    return str(ws.id)


async def _fund(workspace_id: str, credits: int) -> None:
    from pocketpaw_ee.cloud.credits import service as credits_service

    await credits_service.grant(
        workspace=workspace_id,
        amount=credits,
        cause="top_up",
        idempotency_key=f"seed-{workspace_id}",
    )


async def _balance(workspace_id: str) -> int:
    from pocketpaw_ee.cloud.credits import service as credits_service

    return await credits_service.balance(workspace_id)


async def _free_site(workspace_id: str) -> str:
    """A pocket published on the free floor — what a partner builds before selling."""
    from pocketpaw_ee.cloud.models.pocket import Pocket
    from pocketpaw_ee.sites import service as sites_service

    pocket = Pocket(
        workspace=workspace_id, name="Ravi Stores", owner="u1", type="site", pattern="landing"
    )
    await pocket.insert()
    doc = await sites_service.publish_pocket(
        workspace_id=workspace_id, user_id="u1", pocket_id=str(pocket.id), site_plan_key="free"
    )
    assert doc.deployed is True and doc.plan_tier == "free"
    return str(doc.id)


async def _client(ctx: RequestContext, store) -> str:
    return (
        await service.create_client(ctx, body={"name": "Ravi", "whatsapp": PHONE}, store=store)
    ).id


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def test_offers_are_priced_for_the_partner_country(mongo_db) -> None:
    india = _ctx(await _partner_ws("in-shop", country="IN"))
    elsewhere = _ctx(await _partner_ws("us-shop", country="US"))

    offers = {o.sku: o for o in await service.list_offers(india)}
    assert {k: o.price_credits for k, o in offers.items()} == {
        "site_year": 1700,
        "staff_year": 5600,
    }
    assert offers["staff_year"].conversation_allowance == 1200
    assert offers["site_year"].period_months == 12
    assert {o.sku: o.price_credits for o in await service.list_offers(elsewhere)} == {
        "site_year": 2900,
        "staff_year": 8900,
    }
    with pytest.raises(Forbidden):
        await service.list_offers(_ctx(str((await _workspace("plain")).id)))


async def test_partner_tiers_stay_out_of_the_public_storefront() -> None:
    from pocketpaw_ee.cloud.billing import site_plans

    public = {t.key for t in site_plans.list_site_plans()}
    assert public == {"free", "site", "staff"}
    assert {t.key for t in site_plans.list_site_scoped_plans()} == public
    assert all(t.partner_only for t in site_plans.list_partner_plans())


async def test_selling_site_year_debits_the_partner_price_and_redeploys(
    mongo_db, store, monkeypatch
) -> None:
    deploys = _sell_seams(monkeypatch)
    wid = await _partner_ws("in-shop")
    ctx = _ctx(wid)
    await _fund(wid, 5000)
    site_id = await _free_site(wid)
    client_id = await _client(ctx, store)
    deploys.clear()

    sale = await service.sell(
        ctx, body={"client_id": client_id, "site_id": site_id, "sku": "site_year"}, store=store
    )

    assert await _balance(wid) == 5000 - 1700, "exactly the IN price, not plan-carried"
    assert sale.plan_tier == "site_year"
    assert sale.subscription_status == "active"
    assert sale.partner_client_id == client_id
    assert deploys, "a sale redeploys the site through the publish path"
    from pocketpaw_ee.cloud.models.site import Site

    doc = await Site.get(site_id)
    assert doc.billing_rail == "credits"
    assert doc.period_paid_usd == 17
    assert doc.partner_client_id == client_id
    expected = datetime.now(UTC) + relativedelta(months=12)
    assert abs((_aware(doc.renewal_date) - expected).total_seconds()) < 3600

    # Same sku, same day: one debit, no second deploy.
    deploys.clear()
    again = await service.sell(
        ctx, body={"client_id": client_id, "site_id": site_id, "sku": "site_year"}, store=store
    )
    assert await _balance(wid) == 5000 - 1700
    assert again.plan_tier == "site_year"
    assert not deploys


async def test_selling_a_year_to_a_monthly_site_is_a_fresh_purchase(
    mongo_db, store, monkeypatch
) -> None:
    """A site already paying $7 for the month moves to a year: a different period
    is a FRESH purchase — the full IN year price, and the year starts today. The
    unused month is not credited (no proration anywhere on this rail)."""
    from pocketpaw_ee.cloud.models.site import Site

    deploys = _sell_seams(monkeypatch)
    wid = await _partner_ws("in-shop")
    ctx = _ctx(wid)
    await _fund(wid, 5000)
    site_id = await _free_site(wid)
    doc = await Site.get(site_id)
    doc.plan_tier, doc.subscription_status, doc.billing_rail = "site", "active", "credits"
    doc.period_paid_usd = 7
    doc.renewal_date = datetime.now(UTC) + timedelta(days=20)
    await doc.save()
    deploys.clear()

    await service.sell(
        ctx,
        body={"client_id": await _client(ctx, store), "site_id": site_id, "sku": "site_year"},
        store=store,
    )

    fresh = await Site.get(site_id)
    assert await _balance(wid) == 5000 - 1700
    assert fresh.plan_tier == "site_year"
    assert fresh.period_paid_usd == 17
    expected = datetime.now(UTC) + relativedelta(months=12)
    assert abs((_aware(fresh.renewal_date) - expected).total_seconds()) < 3600
    assert deploys


async def test_a_short_wallet_refuses_the_sale_and_changes_nothing(
    mongo_db, store, monkeypatch
) -> None:
    from pocketpaw_ee.cloud._core.errors import InsufficientCredits
    from pocketpaw_ee.cloud.models.site import Site

    _sell_seams(monkeypatch)
    wid = await _partner_ws("in-shop")
    ctx = _ctx(wid)
    await _fund(wid, 1000)
    site_id = await _free_site(wid)
    client_id = await _client(ctx, store)

    with pytest.raises(InsufficientCredits) as exc:
        await service.sell(
            ctx, body={"client_id": client_id, "site_id": site_id, "sku": "site_year"}, store=store
        )
    assert exc.value.status_code == 402

    doc = await Site.get(site_id)
    assert await _balance(wid) == 1000
    assert doc.plan_tier == "free"
    assert doc.subscription_status == "none"
    assert doc.deployed is True
    assert doc.partner_client_id is None


async def test_selling_needs_an_active_partner_and_its_own_client_and_site(
    mongo_db, store, monkeypatch
) -> None:
    _sell_seams(monkeypatch)
    wid = await _partner_ws("in-shop")
    other = await _partner_ws("other-shop")
    ctx = _ctx(wid)
    await _fund(wid, 5000)
    site_id = await _free_site(wid)
    client_id = await _client(ctx, store)
    body = {"client_id": client_id, "site_id": site_id, "sku": "site_year"}

    plain = str((await _workspace("plain")).id)
    for status_ws in (plain, await _partner_ws("sus-shop", status="suspended")):
        with pytest.raises(Forbidden):
            await service.sell(_ctx(status_ws), body=body, store=store)
    with pytest.raises(NotFound):  # another partner's client
        await service.sell(
            _ctx(other), body={**body, "site_id": await _free_site(other)}, store=store
        )
    other_client = await _client(_ctx(other), store)
    with pytest.raises(NotFound):  # another workspace's site
        await service.sell(_ctx(other), body={**body, "client_id": other_client}, store=store)
    from pocketpaw_ee.cloud._core.errors import ValidationError

    with pytest.raises(ValidationError):  # a public tier is not a partner sku
        await service.sell(ctx, body={**body, "sku": "staff"}, store=store)
    assert await _balance(wid) == 5000


async def test_a_partner_tier_cannot_be_bought_through_a_normal_publish(
    mongo_db, monkeypatch
) -> None:
    from pocketpaw_ee.cloud.models.pocket import Pocket
    from pocketpaw_ee.sites import service as sites_service

    from tests.cloud.sites.test_credits_publish import _local_deploy

    _local_deploy(monkeypatch)
    ws = WorkspaceDoc(name="plain", slug="plain-go", owner="u1", plan="go")
    await ws.insert()
    wid = str(ws.id)
    await _fund(wid, 10_000)
    pocket = Pocket(workspace=wid, name="P", owner="u1", type="site", pattern="landing")
    await pocket.insert()

    with pytest.raises(Forbidden) as exc:
        await sites_service.publish_pocket(
            workspace_id=wid,
            user_id="u1",
            pocket_id=str(pocket.id),
            site_plan_key="site_year",
            purchase_authorized=True,
            _bundle_reader=lambda d: b"x",
        )
    assert exc.value.code == "sites.partner_plan_only"
    assert await _balance(wid) == 10_000


async def _sold_site(
    workspace_id: str, *, tier: str, renewal_date: datetime | None, client_id: str | None = "c1"
):
    from pocketpaw_ee.cloud.models.site import Site

    doc = Site(
        workspace=workspace_id,
        pocket_id=f"pk_{tier}_{renewal_date}",
        owner="u1",
        name=f"Sold {tier}",
        deployed=True,
        url="http://local/sold/",
        plan_tier=tier,
        subscription_status="active",
        billing_rail="credits",
        renewal_date=renewal_date,
        period_paid_usd=17,
        partner_client_id=client_id,
    )
    await doc.insert()
    return doc


async def test_the_renewal_sweeper_renews_a_year_at_the_partner_price(mongo_db) -> None:
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.sites.renewal_sweeper import sweep_site_renewals

    wid = await _partner_ws("in-shop")
    await _fund(wid, 2000)
    due = datetime.now(UTC) - timedelta(days=1)
    doc = await _sold_site(wid, tier="site_year", renewal_date=due)

    assert (await sweep_site_renewals())["renewed"] == 1
    fresh = await Site.get(doc.id)
    assert await _balance(wid) == 2000 - 1700
    step = _aware(fresh.renewal_date) - (_aware(due) + relativedelta(months=12))
    assert abs(step.total_seconds()) < 1  # BSON keeps milliseconds only
    assert fresh.period_paid_usd == 17


async def test_a_short_wallet_lapses_a_partner_site_and_keeps_it_up(mongo_db) -> None:
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.sites.renewal_sweeper import sweep_site_renewals

    wid = await _partner_ws("in-shop")
    await _fund(wid, 1000)
    doc = await _sold_site(
        wid, tier="staff_year", renewal_date=datetime.now(UTC) - timedelta(days=1)
    )

    assert (await sweep_site_renewals())["lapsed"] == 1
    fresh = await Site.get(doc.id)
    assert fresh.subscription_status == "cancelled"
    assert fresh.deployed is True
    assert await _balance(wid) == 1000


async def test_partner_tiers_resolve_their_entitlements() -> None:
    from pocketpaw_ee.cloud.billing import site_plans
    from pocketpaw_ee.cloud.entitlements import service as ent

    def resolve(tier: str):
        return ent.resolve_site_entitlements(
            site_id="s",
            workspace_id="w",
            plan_tier=tier,
            subscription_status="active",
            concierge_enabled=True,
        )

    staff_year = resolve("staff_year")
    assert staff_year.concierge_entitled is True
    assert site_plans.site_scoped_tier("staff_year").conversation_allowance == 1200
    site_year = resolve("site_year")
    assert site_year.badge_required is False
    assert site_year.custom_domain is True
    assert site_year.concierge_entitled is False


async def test_the_staff_year_quota_counts_over_the_paid_year(monkeypatch) -> None:
    monkeypatch.setattr(
        "pocketpaw.config.get_settings",
        lambda: SimpleNamespace(billing_enforced=True, sites_billing_enforced=False),
    )
    seen: list[datetime] = []

    class _Store:
        async def count_conversations_started_since(self, widget_id, since, workspace_id):
            seen.append(since)
            return 1199

    renewal = datetime(2027, 3, 15, tzinfo=UTC)
    site = SimpleNamespace(plan_tier="staff_year", renewal_date=renewal)
    exceeded = await enforcement.concierge_conversation_quota_exceeded(
        site, widget_id="w", workspace_id="ws", store=_Store()
    )
    assert exceeded is False  # 1,199 of 1,200 for the year
    expected = (renewal - relativedelta(months=12)).astimezone().replace(tzinfo=None)
    assert seen == [expected]


async def test_partner_sites_list_all_or_only_the_due(mongo_db, store) -> None:
    wid = await _partner_ws("in-shop")
    ctx = _ctx(wid)
    client_id = await _client(ctx, store)
    soon = await _sold_site(
        wid,
        tier="site_year",
        renewal_date=datetime.now(UTC) + timedelta(days=10),
        client_id=client_id,
    )
    later = await _sold_site(
        wid, tier="staff_year", renewal_date=datetime.now(UTC) + timedelta(days=200)
    )
    lapsed = await _sold_site(wid, tier="site_year", renewal_date=None)
    await _sold_site(wid, tier="site", renewal_date=datetime.now(UTC), client_id=None)  # not sold

    every = await service.list_sites(ctx, store=store)
    assert {s.site_id for s in every} == {str(soon.id), str(later.id), str(lapsed.id)}
    due = await service.list_sites(ctx, due_within_days=30, store=store)
    assert [s.site_id for s in due] == [str(soon.id)]
    assert due[0].client_name == "Ravi"


async def test_http_selling_needs_the_buy_plan_action(partners_http) -> None:
    client, holder, wid = partners_http
    body = {"client_id": "c", "site_id": "s", "sku": "nope"}
    holder["user"] = _user(wid, "member")
    assert (await client.post("/api/v1/partners/sell", json=body)).status_code == 403
    assert (await client.get("/api/v1/partners/offers")).status_code == 200
    assert (await client.get("/api/v1/partners/sites?due_within_days=30")).status_code == 200
    holder["user"] = _user(wid, "admin")
    r = await client.post("/api/v1/partners/sell", json=body)
    assert r.status_code == 422, r.text  # past the guard, refused on the unknown sku


# ------------------------------------------------- period changes (review B1)


async def _sold_year(monkeypatch, store, *, renewal_in_days: int):
    """A US partner site sold ``site_year`` ($29), renewal moved ``renewal_in_days``
    out. Returns (workspace id, ctx, site id, client id, balance after the sale)."""
    from pocketpaw_ee.cloud.models.site import Site

    _sell_seams(monkeypatch)
    wid = await _partner_ws("us-shop", country="US")
    ctx = _ctx(wid)
    await _fund(wid, 10_000)
    site_id = await _free_site(wid)
    cid = await _client(ctx, store)
    await service.sell(
        ctx, body={"client_id": cid, "site_id": site_id, "sku": "site_year"}, store=store
    )
    assert await _balance(wid) == 10_000 - 2900
    doc = await Site.get(site_id)
    doc.renewal_date = datetime.now(UTC) + timedelta(days=renewal_in_days)
    await doc.save()
    return wid, ctx, site_id, cid, 10_000 - 2900


async def _republish(wid: str, site_id: str, tier: str):
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.sites import service as sites_service

    doc = await Site.get(site_id)
    return await sites_service.publish_pocket(
        workspace_id=wid,
        user_id="u1",
        pocket_id=doc.pocket_id,
        site_plan_key=tier,
        purchase_authorized=True,
    )


@pytest.mark.parametrize("monthly", ["site", "staff"])
async def test_a_running_year_cannot_move_to_a_monthly_plan(
    mongo_db, store, monkeypatch, monthly
) -> None:
    """Both reproduced leaks. ``site``: the year -> month -> year flip used to restart
    a year for nothing. ``staff``: $19 < $29 meant no charge and a free concierge
    until the yearly renewal. Refused now, and nothing about the site changes."""
    from pocketpaw_ee.cloud._core.errors import ConflictError
    from pocketpaw_ee.cloud.models.site import Site

    wid, ctx, site_id, cid, balance = await _sold_year(monkeypatch, store, renewal_in_days=5)
    before = await Site.get(site_id)

    with pytest.raises(ConflictError) as exc:
        await _republish(wid, site_id, monthly)
    assert exc.value.code == "sites.period_downgrade_refused"

    after = await Site.get(site_id)
    assert await _balance(wid) == balance
    assert (after.plan_tier, after.period_paid_usd, after.subscription_status) == (
        "site_year",
        29,
        "active",
    )
    assert after.renewal_date == before.renewal_date

    # Re-selling the year is a no-op: no charge, and the year does NOT restart.
    await service.sell(
        ctx, body={"client_id": cid, "site_id": site_id, "sku": "site_year"}, store=store
    )
    again = await Site.get(site_id)
    assert await _balance(wid) == balance
    assert _aware(again.renewal_date) < datetime.now(UTC) + timedelta(days=6)


async def test_a_lapsed_year_moves_and_comes_back_only_by_paying(
    mongo_db, store, monkeypatch
) -> None:
    """Once a year bought on an earlier day has run out (the sweep has not got to it
    yet), moving to monthly is a fresh $7 purchase and going back to the year a
    fresh $29 one. The period restarts only on a charge."""
    from pocketpaw_ee.cloud.models.site import Site

    _sell_seams(monkeypatch)
    wid = await _partner_ws("us-shop", country="US")
    ctx = _ctx(wid)
    await _fund(wid, 10_000)
    site_id = await _free_site(wid)
    cid = await _client(ctx, store)
    doc = await Site.get(site_id)  # a year bought last year, now just past due
    doc.plan_tier, doc.subscription_status, doc.billing_rail = "site_year", "active", "credits"
    doc.period_paid_usd = 29
    doc.renewal_date = datetime.now(UTC) - timedelta(days=1)
    await doc.save()

    await _republish(wid, site_id, "site")
    monthly = await Site.get(site_id)
    assert await _balance(wid) == 10_000 - 700
    assert (monthly.plan_tier, monthly.period_paid_usd) == ("site", 7)
    month_out = datetime.now(UTC) + relativedelta(months=1)
    assert abs((_aware(monthly.renewal_date) - month_out).total_seconds()) < 3600

    body = {"client_id": cid, "site_id": site_id, "sku": "site_year"}
    await service.sell(ctx, body=body, store=store)
    yearly = await Site.get(site_id)
    assert await _balance(wid) == 10_000 - 700 - 2900
    assert (yearly.plan_tier, yearly.period_paid_usd) == ("site_year", 29)


async def test_a_same_day_rebuy_cannot_replay_the_debit_into_a_new_period(
    mongo_db, store, monkeypatch
) -> None:
    """The debit key is per (site, tier, day), so buying the year back on the day it
    was first bought would replay the debit as a silent no-op. The period must not
    restart for that: the re-buy is refused."""
    from pocketpaw_ee.cloud._core.errors import ConflictError
    from pocketpaw_ee.cloud.models.site import Site

    wid, ctx, site_id, cid, balance = await _sold_year(monkeypatch, store, renewal_in_days=-1)
    await _republish(wid, site_id, "site")  # past due, so a fresh $7 month
    assert await _balance(wid) == balance - 700

    with pytest.raises(ConflictError) as exc:
        await service.sell(
            ctx, body={"client_id": cid, "site_id": site_id, "sku": "site_year"}, store=store
        )
    assert exc.value.code == "sites.plan_already_bought_today"
    doc = await Site.get(site_id)
    assert await _balance(wid) == balance - 700
    assert (doc.plan_tier, doc.period_paid_usd) == ("site", 7)


async def test_a_renewal_without_a_partner_profile_keeps_the_last_price(mongo_db) -> None:
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.sites.renewal_sweeper import sweep_site_renewals

    ws = WorkspaceDoc(name="gone", slug="gone-partner", owner="u1", plan="go")
    await ws.insert()  # no partner profile any more
    wid = str(ws.id)
    await _fund(wid, 5000)
    due = datetime.now(UTC) - timedelta(days=1)
    kept = await _sold_site(wid, tier="site_year", renewal_date=due)  # paid $17 last year

    assert (await sweep_site_renewals())["renewed"] == 1
    assert await _balance(wid) == 5000 - 1700, "the price last paid, not the $29 default"
    assert (await Site.get(kept.id)).period_paid_usd == 17


async def test_a_renewal_with_no_price_to_keep_is_refused_not_guessed(mongo_db) -> None:
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.sites.renewal_sweeper import sweep_site_renewals

    ws = WorkspaceDoc(name="gone", slug="gone-partner-2", owner="u1", plan="go")
    await ws.insert()
    wid = str(ws.id)
    await _fund(wid, 5000)
    due = datetime.now(UTC) - timedelta(days=1)
    doc = await _sold_site(wid, tier="site_year", renewal_date=due)
    doc.period_paid_usd = 0
    await doc.save()

    counts = await sweep_site_renewals()
    assert counts["failed"] == 1 and counts["renewed"] == 0
    fresh = await Site.get(doc.id)
    assert await _balance(wid) == 5000
    assert fresh.subscription_status == "active", "left due for an operator, not lapsed"
    assert _aware(fresh.renewal_date) == _aware(due).replace(
        microsecond=_aware(fresh.renewal_date).microsecond
    )


async def test_a_partner_tier_cannot_be_requested_through_the_plan_request_door(mongo_db) -> None:
    from pocketpaw_ee.cloud.site_plan_requests import propose_site_plan_request

    with pytest.raises(ValueError):
        await propose_site_plan_request(
            workspace_id="ws", pocket_id="pk", site_plan_key="site_year", requested_by="u1"
        )
