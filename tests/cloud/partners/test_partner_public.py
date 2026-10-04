# tests/cloud/partners/test_partner_public.py — Paw Partners public profile (PW-7).
#
# Locks the contract: a partner edits its own public profile (round-trip through
# /me), slug rules (pattern, reserved words, uniqueness, public needs slug and
# name), an operator PUT keeps the partner's public fields, the directory and
# /partners/{slug} show only active + public partners and never a private field,
# the fixed /partners segments still resolve with the slug catch-all in place,
# apply stores exactly one PartnerApplication (none when Turnstile refuses or the
# global daily cap is spent), operators list and review the queue through
# /platform/partners/applications (support cannot), the slug unique-index race
# is a 409, and the two public buckets return 429.

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import rate_limit
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import BadRequest, ConflictError, NotFound, ValidationError
from pocketpaw_ee.cloud.discover import service_admin as discover_admin
from pocketpaw_ee.cloud.models.partner_application import PartnerApplication
from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
from pocketpaw_ee.cloud.models.workspace import PartnerProfile
from pocketpaw_ee.cloud.models.workspace import Workspace as WorkspaceDoc
from pocketpaw_ee.cloud.partners import service, service_admin
from pocketpaw_ee.cloud.partners.domain import PARTNER_SLUG_RESERVED
from pocketpaw_ee.cloud.partners.dto import PartnerPublicOut
from pydantic import ValidationError as PydanticValidationError
from pymongo.errors import DuplicateKeyError

pytestmark = [pytest.mark.usefixtures("mongo_db"), pytest.mark.asyncio]

URL = "/api/v1/partners"
PRIVATE = {"footer_name", "billing_country", "founding", "status"}
PROFILE = {
    "slug": "ravi-prints",
    "display_name": "Ravi Prints",
    "city": "Bengaluru",
    "country": "in",
    "services": ["print", "design"],
    "bio": "Flex and vinyl since 2009.",
    "contact_url": "https://wa.me/919876543210",
    "public": True,
}
APPLY = {
    "name": "Meera Designs",
    "email": "meera@example.com",
    "city": "Pune",
    "country": "IN",
    "services": ["design", "web"],
    "message": "We build sites for 40 shops a year.",
    "turnstile_token": "tok",
}


def _ctx(workspace_id: str | None) -> RequestContext:
    return RequestContext(
        user_id="u1",
        workspace_id=workspace_id,
        request_id="r1",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


async def _workspace(
    name: str, status: str | None = None, public: dict[str, Any] | None = None
) -> WorkspaceDoc:
    ws = WorkspaceDoc(name=name, slug=name, owner="u1")
    if status:
        ws.partner = PartnerProfile(status=status, footer_name=f"{name} Prints", **(public or {}))
    await ws.insert()
    return ws


async def _public_partner(slug: str, status: str = "active", **extra: Any) -> WorkspaceDoc:
    return await _workspace(
        slug, status, {**PROFILE, "slug": slug, "display_name": slug.title(), **extra}
    )


@pytest.fixture(autouse=True)
def _discover_sources() -> None:
    from pocketpaw_ee.cloud.discover import sources

    sources.register_builtin_sources()


@pytest.fixture(autouse=True)
def _fresh_buckets():
    rate_limit._partner_public_limiter._buckets.clear()
    rate_limit._partner_apply_limiter._buckets.clear()
    yield
    rate_limit._partner_public_limiter._buckets.clear()
    rate_limit._partner_apply_limiter._buckets.clear()


@pytest_asyncio.fixture
async def http():
    """The real partners router: anonymous unless ``act_as`` is called."""
    from pocketpaw_ee.cloud._core.context import request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.partners.router import router

    state = SimpleNamespace(user=None, wid=None)

    async def _user():
        if state.user is None:
            raise HTTPException(401, "Unauthorized")
        return state.user

    def act_as(wid: str, role: str | None = "member") -> None:
        state.wid = wid
        state.user = SimpleNamespace(
            id="u1",
            active_workspace=wid,
            workspaces=[SimpleNamespace(workspace=wid, role=role)] if role else [],
        )

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_active_user] = _user
    app.dependency_overrides[current_workspace_id] = lambda: state.wid
    app.dependency_overrides[request_context] = lambda: _ctx(state.wid)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.act_as = act_as
        yield c


async def _applications() -> list[PartnerApplication]:
    return await PartnerApplication.find_all().sort([("_id", 1)]).to_list()


# ---------------------------------------------------------------- own profile


async def test_profile_round_trip_through_me(http) -> None:
    ws = await _workspace("acme", "active")
    http.act_as(str(ws.id))

    r = await http.patch(f"{URL}/me/profile", json=PROFILE)
    assert r.status_code == 200, r.text
    me = r.json()
    assert me["slug"] == "ravi-prints" and me["public"] is True
    assert me["country"] == "IN"  # normalised
    assert me["services"] == ["print", "design"]

    again = (await http.get(f"{URL}/me")).json()
    assert {k: again[k] for k in PROFILE} == {**PROFILE, "country": "IN"}
    stored = (await WorkspaceDoc.get(ws.id)).partner
    assert stored.footer_name == "acme Prints" and stored.status == "active"  # untouched

    # PATCH changes only what is sent; an empty PATCH is a no-op.
    r = await http.patch(f"{URL}/me/profile", json={"bio": None, "public": False})
    assert r.json()["bio"] is None and r.json()["public"] is False
    assert r.json()["slug"] == "ravi-prints"
    assert (await http.patch(f"{URL}/me/profile", json={})).json()["slug"] == "ravi-prints"


async def test_profile_needs_a_partner_and_fabric_write(http) -> None:
    ws = await _workspace("plain")
    http.act_as(str(ws.id))
    assert (await http.patch(f"{URL}/me/profile", json={"bio": "x"})).status_code == 404
    partner = await _workspace("acme", "applied")
    http.act_as(str(partner.id))
    # An applied partner may fill in its profile ahead of activation.
    assert (await http.patch(f"{URL}/me/profile", json={"bio": "x"})).status_code == 200
    http.act_as(str(partner.id), role=None)  # signed in, not a member here
    assert (await http.patch(f"{URL}/me/profile", json={"bio": "y"})).status_code == 403


@pytest.mark.parametrize("slug", ["Ravi", "ab", "a" * 61, "ravi prints", "ravi_prints"])
async def test_slug_pattern(http, slug) -> None:
    ws = await _workspace("acme", "active")
    http.act_as(str(ws.id))
    r = await http.patch(f"{URL}/me/profile", json={"slug": slug})
    assert r.status_code == 422, r.text


async def test_route_segments_stay_reserved() -> None:
    # API fixed segments plus the public site's /partners/find page.
    assert {"directory", "apply", "find"} <= PARTNER_SLUG_RESERVED


@pytest.mark.parametrize("slug", sorted(PARTNER_SLUG_RESERVED))
async def test_reserved_slugs_are_refused(slug) -> None:
    ws = await _workspace("acme", "active")
    with pytest.raises(PydanticValidationError) as exc:  # the wire DTO: 422 at the route
        await service.update_public_profile(_ctx(str(ws.id)), {"slug": slug})
    # "me" already fails the 3-char minimum; every other reserved word is rejected by name.
    assert slug == "me" or "reserved" in str(exc.value)
    # The storage model refuses them too, so no other writer can slip one in.
    with pytest.raises(PydanticValidationError):
        PartnerProfile(status="active", footer_name="x", slug=slug)


async def test_slug_is_unique_across_workspaces() -> None:
    await _public_partner("ravi-prints")
    other = await _workspace("other", "active")
    with pytest.raises(ConflictError) as exc:
        await service.update_public_profile(_ctx(str(other.id)), {"slug": "ravi-prints"})
    assert exc.value.code == "partners.slug_taken"
    # Keeping your own slug is not a collision.
    me = await service.update_public_profile(
        _ctx(str(other.id)), {"slug": "other-prints", "display_name": "Other"}
    )
    assert (
        await service.update_public_profile(_ctx(str(other.id)), {"slug": "other-prints"})
    ).slug == me.slug


async def test_public_needs_slug_and_display_name() -> None:
    ws = await _workspace("acme", "active")
    ctx = _ctx(str(ws.id))
    with pytest.raises(ValidationError) as exc:
        await service.update_public_profile(ctx, {"public": True})
    assert exc.value.code == "partners.profile_incomplete"
    with pytest.raises(PydanticValidationError):  # closed service set, 422 at the route
        await service.update_public_profile(ctx, {"services": ["printing"]})
    assert (await service.update_public_profile(ctx, PROFILE)).public is True


@pytest_asyncio.fixture
async def platform_http():
    """The real platform router and guard; only the session user is faked.
    An operator by default; ``act_as("support")`` swaps in a support user."""
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.models.user import User as UserDoc
    from pocketpaw_ee.cloud.platform.router import router as platform_router

    users = {}
    for role in ("operator", "support"):
        users[role] = UserDoc(email=f"{role}@paw.test", hashed_password="x", platform_role=role)
        await users[role].insert()
    state = SimpleNamespace(user=users["operator"])
    app = FastAPI()
    add_error_handler(app)
    app.include_router(platform_router, prefix="/api/v1")
    app.dependency_overrides[current_active_user] = lambda: state.user
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t", cookies={"paw_auth": "session"}
    ) as c:
        c.act_as = lambda role: setattr(state, "user", users[role])
        c.operator_id = str(users["operator"].id)
        yield c


async def test_operator_put_keeps_the_public_profile(platform_http) -> None:
    """A status / tier change by an operator is not a reset of the partner's own fields."""
    ws = await _public_partner("ravi-prints")
    r = await platform_http.put(
        f"/api/v1/platform/workspaces/{ws.id}/partner",
        json={"status": "suspended", "footer_name": "Ravi", "reason": "late"},
    )
    assert r.status_code == 200, r.text
    kept = (await WorkspaceDoc.get(ws.id)).partner
    assert kept.status == "suspended" and kept.footer_name == "Ravi"
    assert kept.slug == "ravi-prints" and kept.public is True
    assert kept.services == ["print", "design"] and kept.contact_url == PROFILE["contact_url"]
    # Suspended, so no longer public on the wire, but the profile is intact for reactivation.
    with pytest.raises(NotFound):
        await service_admin.get_public("ravi-prints")


# ---------------------------------------------------------------- directory + slug


async def test_directory_shows_only_active_public_partners(http) -> None:
    shown = await _public_partner("ravi-prints")
    await _public_partner("opted-out", public=False)
    await _public_partner("suspended-one", status="suspended")
    await _public_partner("applied-one", status="applied")
    await _workspace("plain")

    r = await http.get(f"{URL}/directory")
    assert r.status_code == 200, r.text
    body = r.json()
    assert [p["slug"] for p in body["items"]] == ["ravi-prints"]
    assert body["next_cursor"] is None
    card = body["items"][0]
    assert set(card) == set(PartnerPublicOut.model_fields)
    assert not (set(card) & PRIVATE)
    assert card["display_name"] == "Ravi-Prints" and card["tier"] == "bronze"
    assert card["sites"] == []

    for slug in ("opted-out", "suspended-one", "applied-one", "nope"):
        assert (await http.get(f"{URL}/{slug}")).status_code == 404, slug
    one = await http.get(f"{URL}/ravi-prints")
    assert one.status_code == 200 and one.json()["slug"] == "ravi-prints"
    assert not (set(one.json()) & PRIVATE)
    assert str(shown.id) not in one.text


async def test_directory_filters_and_pages(http) -> None:
    a = await _public_partner("a-prints", city="Pune", services=["print"])
    b = await _public_partner("b-web", city="pune", services=["web"])
    c = await _public_partner("c-photo", city="Mumbai", services=["photo", "web"])

    slugs = lambda r: [p["slug"] for p in r.json()["items"]]  # noqa: E731
    assert slugs(await http.get(f"{URL}/directory", params={"city": "PUNE"})) == [
        "b-web",
        "a-prints",
    ]
    assert slugs(await http.get(f"{URL}/directory", params={"service": "web"})) == [
        "c-photo",
        "b-web",
    ]
    assert (await http.get(f"{URL}/directory", params={"service": "tattoo"})).status_code == 422

    first = await http.get(f"{URL}/directory", params={"limit": 2})
    assert slugs(first) == ["c-photo", "b-web"] and first.json()["next_cursor"] == str(b.id)
    second = await http.get(f"{URL}/directory", params={"limit": 2, "cursor": str(b.id)})
    assert slugs(second) == ["a-prints"] and second.json()["next_cursor"] is None
    assert (await http.get(f"{URL}/directory", params={"cursor": "junk"})).status_code == 422
    assert a.id != c.id


async def test_public_profile_lists_the_partners_discover_sites(http) -> None:
    ws = await _public_partner("ravi-prints")
    other = await _public_partner("other-one")
    listed = await discover_admin.upsert_from_source(
        "site_template",
        "t1",
        {"workspace": str(ws.id), "owner": "u1", "kind": "site", "title": "Bakery"},
    )
    await discover_admin.upsert_from_source(
        "site_template",
        "t2",
        {"workspace": str(ws.id), "owner": "u1", "kind": "site", "title": "Hidden"},
        hide=True,
    )
    await discover_admin.upsert_from_source(
        "site_template",
        "t3",
        {"workspace": str(other.id), "owner": "u1", "kind": "site", "title": "Theirs"},
    )

    sites = (await http.get(f"{URL}/ravi-prints")).json()["sites"]
    assert [s["id"] for s in sites] == [listed]
    assert sites[0]["title"] == "Bakery" and "workspace" not in sites[0]
    page = {p["slug"]: p["sites"] for p in (await http.get(f"{URL}/directory")).json()["items"]}
    assert [s["title"] for s in page["ravi-prints"]] == ["Bakery"]
    assert [s["title"] for s in page["other-one"]] == ["Theirs"]


async def test_public_profile_shows_only_the_newest_sites(http) -> None:
    """One partner with many listings cannot bloat the directory page."""
    ws = await _public_partner("ravi-prints")
    cap = service_admin.SITES_PER_PARTNER
    for i in range(cap + 3):
        await discover_admin.upsert_from_source(
            "site_template",
            f"t{i:02d}",
            {"workspace": str(ws.id), "owner": "u1", "kind": "site", "title": f"Site {i:02d}"},
        )
    for path in (f"{URL}/ravi-prints", f"{URL}/directory"):
        body = (await http.get(path)).json()
        sites = body["sites"] if "sites" in body else body["items"][0]["sites"]
        assert len(sites) == cap == 12
        assert [s["title"] for s in sites] == [f"Site {i:02d}" for i in range(cap + 2, 2, -1)]


async def test_fixed_segments_win_over_the_slug_catch_all(http) -> None:
    await _public_partner("ravi-prints")
    # Anonymous: the fixed routes answer 401 (auth), not 404 from the catch-all.
    for seg in ("me", "clients", "offers", "sites", "summary", "earnings", "rewards"):
        assert (await http.get(f"{URL}/{seg}")).status_code == 401, seg
    assert (await http.get(f"{URL}/directory")).status_code == 200
    ws = await _workspace("acme", "active")
    http.act_as(str(ws.id))
    assert (await http.get(f"{URL}/me")).json()["status"] == "active"


# ---------------------------------------------------------------- apply


async def test_apply_stores_exactly_one_application(http) -> None:
    r = await http.post(f"{URL}/apply", json=APPLY, headers={"x-forwarded-for": "203.0.113.7"})
    assert r.status_code == 204, r.text
    rows = await _applications()
    assert len(rows) == 1
    row = rows[0]
    assert (row.name, row.email, row.city, row.country) == (
        "Meera Designs",
        "meera@example.com",
        "Pune",
        "IN",
    )
    assert row.services == ["design", "web"] and row.message == APPLY["message"]
    assert row.status == "new" and row.reviewed_by is None and row.reviewed_at is None
    # The address is stored hashed, never raw.
    assert row.source_ip_hash == hashlib.sha256(b"203.0.113.7").hexdigest()
    assert "203.0.113.7" not in row.model_dump_json()
    # Nothing landed in any tenant: no workspace was created or touched.
    assert await WorkspaceDoc.count() == 0

    bad = {**APPLY, "email": "not-an-email"}
    assert (await http.post(f"{URL}/apply", json=bad)).status_code == 422
    assert (await http.post(f"{URL}/apply", json={**APPLY, "services": []})).status_code == 422
    assert len(await _applications()) == 1


async def test_apply_refused_by_turnstile_stores_nothing(http, monkeypatch) -> None:
    async def refuse(token, ip, *, code, transport=None):
        raise BadRequest(code, "nope")

    monkeypatch.setattr(service_admin, "verify_turnstile", refuse)
    r = await http.post(f"{URL}/apply", json=APPLY)
    assert r.status_code == 400 and r.json()["error"]["code"] == "partners.turnstile_failed"
    assert await _applications() == []


async def test_apply_global_daily_cap(http, monkeypatch) -> None:
    """Many addresses under the per-IP limit still cannot fill the queue past the cap."""
    monkeypatch.setattr(service_admin, "APPLY_DAILY_CAP", 2)
    for i in range(2):
        ip = {"x-forwarded-for": f"203.0.113.{i + 1}"}
        assert (await http.post(f"{URL}/apply", json=APPLY, headers=ip)).status_code == 204
    other = {"x-forwarded-for": "203.0.113.9"}
    blocked = await http.post(f"{URL}/apply", json=APPLY, headers=other)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "partners.apply_daily_limit"
    assert len(await _applications()) == 2
    # Yesterday's rows do not count against today.
    for row in await _applications():
        row.createdAt = row.createdAt - timedelta(days=1)
        await row.save()
    assert (await http.post(f"{URL}/apply", json=APPLY)).status_code == 204


# ---------------------------------------------------------------- operator queue


PLATFORM_APPS = "/api/v1/platform/partners/applications"


async def test_operator_lists_and_reviews_applications(http, platform_http) -> None:
    for name in ("One", "Two", "Three"):
        assert (await http.post(f"{URL}/apply", json={**APPLY, "name": name})).status_code == 204
    r = await platform_http.get(PLATFORM_APPS)
    assert r.status_code == 200, r.text
    page = r.json()
    assert [a["name"] for a in page["items"]] == ["Three", "Two", "One"]
    assert page["next_cursor"] is None
    first = page["items"][0]
    assert first["email"] == "meera@example.com" and first["status"] == "new"
    assert set(first) == {
        "id",
        "name",
        "email",
        "city",
        "country",
        "services",
        "message",
        "status",
        "note",
        "reviewed_by",
        "reviewed_at",
        "created_at",
    }

    # Paging and the status filter.
    two = (await platform_http.get(PLATFORM_APPS, params={"limit": 2})).json()
    assert [a["name"] for a in two["items"]] == ["Three", "Two"] and two["next_cursor"]
    rest = (await platform_http.get(PLATFORM_APPS, params={"cursor": two["next_cursor"]})).json()
    assert [a["name"] for a in rest["items"]] == ["One"]
    assert (await platform_http.get(PLATFORM_APPS, params={"cursor": "junk"})).status_code == 422

    r = await platform_http.patch(
        f"{PLATFORM_APPS}/{first['id']}",
        json={"status": "contacted", "note": "Called, call back Monday", "reason": "triage"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "contacted" and r.json()["note"] == "Called, call back Monday"
    assert r.json()["reviewed_by"] == platform_http.operator_id and r.json()["reviewed_at"]
    fresh = (await platform_http.get(PLATFORM_APPS, params={"status": "new"})).json()
    assert [a["name"] for a in fresh["items"]] == ["Two", "One"]
    assert (await platform_http.get(PLATFORM_APPS, params={"status": "bogus"})).status_code == 422

    # Every write leaves an applied audit row; the list read is recorded too.
    writes = await PlatformAuditEvent.find(
        {"target_type": "partner_application", "status": "applied", "reason": "triage"}
    ).to_list()
    assert len(writes) == 1 and writes[0].after["status"] == "contacted"
    assert await PlatformAuditEvent.find({"action": "platform.partners.write"}).count() >= 5

    # A blank reason is 422 and writes nothing; a missing id is 404 without an audit row.
    before = await PlatformAuditEvent.count()
    r = await platform_http.patch(f"{PLATFORM_APPS}/{first['id']}", json={"status": "rejected"})
    assert r.status_code == 422
    reject = {"status": "rejected", "reason": "x"}
    for missing in ("0" * 24, "nope"):
        r = await platform_http.patch(f"{PLATFORM_APPS}/{missing}", json=reject)
        assert r.status_code == 404, missing
    assert await PlatformAuditEvent.count() == before


async def test_support_cannot_see_or_review_applications(http, platform_http) -> None:
    assert (await http.post(f"{URL}/apply", json=APPLY)).status_code == 204
    app_id = str((await _applications())[0].id)
    platform_http.act_as("support")
    assert (await platform_http.get(PLATFORM_APPS)).status_code == 403
    r = await platform_http.patch(
        f"{PLATFORM_APPS}/{app_id}", json={"status": "accepted", "reason": "x"}
    )
    assert r.status_code == 403
    assert (await _applications())[0].status == "new"


async def test_slug_race_lost_at_the_index_is_a_409(http, monkeypatch) -> None:
    """mongomock ignores the partial unique index, so the DuplicateKeyError path is driven here."""
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    async def lost_race(workspace_id, fields):
        raise DuplicateKeyError("E11000 duplicate key error: partner_slug_1")

    monkeypatch.setattr(workspace_service, "set_partner_public_profile", lost_race)
    ws = await _workspace("acme", "active")
    http.act_as(str(ws.id))
    r = await http.patch(f"{URL}/me/profile", json={"slug": "ravi-prints"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "partners.slug_taken"


# ---------------------------------------------------------------- rate limits


async def test_public_reads_are_rate_limited_per_ip(http) -> None:
    await _public_partner("ravi-prints")
    capacity = rate_limit._partner_public_limiter.capacity
    ip = {"x-forwarded-for": "203.0.113.7"}
    for i in range(capacity):
        path = f"{URL}/directory" if i % 2 else f"{URL}/ravi-prints"
        assert (await http.get(path, headers=ip)).status_code == 200
    blocked = await http.get(f"{URL}/directory", headers=ip)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "partners.rate_limited"
    assert (
        await http.get(f"{URL}/directory", headers={"x-forwarded-for": "203.0.113.8"})
    ).status_code == 200


async def test_apply_is_rate_limited_per_ip(http) -> None:
    capacity = rate_limit._partner_apply_limiter.capacity
    assert capacity == 5
    ip = {"x-forwarded-for": "203.0.113.7"}
    for _ in range(capacity):
        assert (await http.post(f"{URL}/apply", json=APPLY, headers=ip)).status_code == 204
    blocked = await http.post(f"{URL}/apply", json=APPLY, headers=ip)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "partners.apply_rate_limited"
    assert len(await _applications()) == capacity


async def test_service_admin_reads_refuse_hidden_partners_directly() -> None:
    await _public_partner("opted-out", public=False)
    with pytest.raises(NotFound):
        await service_admin.get_public("opted-out")
    assert (await service_admin.list_directory()).items == []
