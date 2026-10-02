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
# Updated 2026-10-02: the autouse fixture clears the shared
# ``read_model.default_journal_store`` cache (``service._default_store`` now
# delegates to it) and points the per-workspace stores at tmp_path.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
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
