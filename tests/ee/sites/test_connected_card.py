# tests/ee/sites/test_connected_card.py — a CONNECTED site's gallery card gets the
# customer's own page title, icon and screenshot.
#
# A connected (``foreign_origin``) site never deploys, so none of the hosted card
# lanes (post-deploy screenshot, post-deploy favicon) ever ran for it, and nothing
# recorded what the customer's page calls itself. ``sites.connected_card`` is the
# one refresh that does all three from a single homepage fetch; these tests pin:
#
#   * WHEN it runs: a first bind, a rebind, an origin (re-)verification, and the
#     owner's preview-refresh.
#   * WHAT it records: ``origin_title`` from <title>, falling back to og:title,
#     sanitised and capped, and NEVER over the owner's ``name``.
#   * HOW it reaches the customer's host: only through ``safe_fetch`` (DNS-pinned,
#     private addresses refused, every redirect hop re-checked). The icon lookup
#     must not fall back to favicon's plain httpx client for a foreign host.
#
# All network is mocked: an ``httpx.MockTransport`` plus a dict-backed resolver.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from bson import ObjectId
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.site_origin_claim import SiteOriginClaim
from pocketpaw_ee.sites import connected_card, favicon
from pocketpaw_ee.sites import screenshot as screenshot_mod
from pocketpaw_ee.sites import service as sites_service

pytestmark = pytest.mark.asyncio

_HOST = "brew.example"
_PUBLIC = {_HOST: ["93.184.216.34"], "www.brew.example": ["93.184.216.35"]}

# A real 1x1 PNG: the favicon gate sniffs magic bytes, so a placeholder would be
# dropped and the icon assertions would pass for the wrong reason.
_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)


def _resolver(table: dict[str, list[str]]):
    async def resolve(host: str) -> list[str]:
        if host not in table:
            raise OSError(f"no DNS for {host}")
        return table[host]

    return resolve


def _transport(routes: dict[tuple[str, str], httpx.Response], seen: list[httpx.Request]):
    """Route on (Host header, path): a pinned request carries the IP in its URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = (request.headers.get("host", ""), request.url.path)
        return routes.get(key, httpx.Response(404, content=b"nope"))

    return httpx.MockTransport(handler)


def _html(body: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html"}, content=body.encode())


async def _verify(host: str = _HOST, *, workspace: str = "ws1") -> None:
    now = datetime.now(UTC)
    await SiteOriginClaim(
        workspace=workspace,
        host=host,
        token="pawverify-token",
        status="verified",
        issued_at=now,
        expires_at=now + timedelta(days=7),
        issued_by="u1",
        method="well-known",
        verified_at=now,
    ).insert()


async def _connected_site(*, name: str = "My Concierge", **extra: Any) -> Site:
    doc = Site(
        id=ObjectId(),
        workspace="ws1",
        pocket_id=f"pocket-{ObjectId()}",
        owner="u1",
        name=name,
        script_name="",
        deployed=False,
        url="",
        signed_key="k",
        foreign_origin=True,
        allowed_origins=extra.pop("allowed_origins", [_HOST]),
        **extra,
    )
    await doc.insert()
    return doc


@pytest.fixture
def network(monkeypatch):
    """Point the card refresh's safe-fetch seams at a MockTransport. Returns a
    function that installs routes (+ an optional DNS table) and the request log."""
    seen: list[httpx.Request] = []

    def install(routes, table=None):
        monkeypatch.setattr(connected_card, "_transport", _transport(routes, seen))
        monkeypatch.setattr(connected_card, "_resolver", _resolver(table or _PUBLIC))
        return seen

    return install


@pytest.fixture
def no_plain_httpx_for_foreign(monkeypatch):
    """favicon's plain httpx client must never be reached for a customer host."""
    calls: list[str] = []

    async def _forbidden(url: str, **_kw) -> bytes:
        calls.append(url)
        raise AssertionError(f"plain httpx favicon fetch used for {url}")

    monkeypatch.setattr(favicon, "_plain_get", _forbidden)
    return calls


def _record_scheduled(monkeypatch) -> list[Any]:
    scheduled: list[Any] = []

    def _record(coro: Any) -> None:
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(connected_card, "_default_card_scheduler", _record)
    return scheduled


# --------------------------------------------------------------------------- #
# Title + icon from one homepage fetch
# --------------------------------------------------------------------------- #


async def test_refresh_takes_the_title_and_leaves_the_owners_name_alone(
    beanie_test_db, network, no_plain_httpx_for_foreign
):
    await _verify()
    site = await _connected_site(name="Owner Chosen")
    seen = network(
        {
            (_HOST, "/"): _html(
                "<html><head><title>  Brew\n &amp; Co\t Coffee </title>"
                '<meta property="og:title" content="OG Brew">'
                '<link rel="icon" href="/fav.png"></head><body>hi</body></html>'
            ),
            (_HOST, "/fav.png"): httpx.Response(200, content=_PNG),
        }
    )

    assert await connected_card.refresh_card_meta(site) is True

    fresh = await Site.get(site.id)
    assert fresh.origin_title == "Brew & Co Coffee"
    assert fresh.name == "Owner Chosen"
    assert fresh.favicon_url.startswith("data:image/png;base64,")
    # One homepage fetch, plus the icon file.
    paths = [r.url.path for r in seen]
    assert paths.count("/") == 1
    assert "/fav.png" in paths
    assert no_plain_httpx_for_foreign == []


async def test_refresh_falls_back_to_og_title(beanie_test_db, network):
    await _verify()
    site = await _connected_site()
    network(
        {
            (_HOST, "/"): _html(
                '<html><head><title>   </title><meta property="og:title" content="OG Brew">'
                "</head></html>"
            )
        }
    )

    await connected_card.refresh_card_meta(site)

    assert (await Site.get(site.id)).origin_title == "OG Brew"


async def test_title_is_sanitised_and_capped():
    raw = "A\x00B\x1b[31m C​" + "x" * 500
    cleaned = connected_card.clean_title(raw)
    assert "\x00" not in cleaned and "\x1b" not in cleaned and "​" not in cleaned
    assert cleaned.startswith("AB[31m C")
    assert len(cleaned) == connected_card.TITLE_MAX_CHARS == 200
    assert connected_card.clean_title("  a \n\n b\t c ") == "a b c"
    assert connected_card.clean_title("") == ""


async def test_a_page_with_no_title_records_empty(beanie_test_db, network):
    await _verify()
    site = await _connected_site(origin_title="Stale")
    network({(_HOST, "/"): _html("<html><body>no head</body></html>")})

    await connected_card.refresh_card_meta(site)

    assert (await Site.get(site.id)).origin_title == ""


async def test_icon_is_fetched_through_safe_fetch_not_plain_httpx(
    beanie_test_db, network, no_plain_httpx_for_foreign
):
    """The /favicon.ico fallback too: it is a request to the customer's host."""
    await _verify()
    site = await _connected_site()
    seen = network(
        {
            (_HOST, "/"): _html("<html><head><title>T</title></head></html>"),
            (_HOST, "/favicon.ico"): httpx.Response(200, content=_PNG),
        }
    )

    await connected_card.refresh_card_meta(site)

    assert (await Site.get(site.id)).favicon_url.startswith("data:image/png")
    assert no_plain_httpx_for_foreign == []
    # Every request rode the pinned path: the URL host is the validated IP.
    assert all(r.url.host == "93.184.216.34" for r in seen)


async def test_a_private_host_is_refused_and_nothing_is_written(beanie_test_db, network):
    await _verify()
    site = await _connected_site(origin_title="Kept")
    seen = network({(_HOST, "/"): _html("<title>Evil</title>")}, table={_HOST: ["10.0.0.7"]})

    assert await connected_card.refresh_card_meta(site) is False

    assert seen == []
    assert (await Site.get(site.id)).origin_title == "Kept"


async def test_a_redirect_to_a_private_host_is_refused(beanie_test_db, network):
    await _verify()
    site = await _connected_site()
    seen = network(
        {
            (_HOST, "/"): httpx.Response(302, headers={"location": "http://internal.example/"}),
            ("internal.example", "/"): _html("<title>Internal</title>"),
        },
        table={**_PUBLIC, "internal.example": ["169.254.169.254"]},
    )

    assert await connected_card.refresh_card_meta(site) is False

    assert [r.headers["host"] for r in seen] == [_HOST]
    assert (await Site.get(site.id)).origin_title == ""


async def test_an_unverified_connected_site_is_not_fetched(beanie_test_db, network):
    site = await _connected_site()
    seen = network({(_HOST, "/"): _html("<title>T</title>")})

    assert await connected_card.refresh_card_meta(site) is False
    assert seen == []


async def test_the_background_refresh_never_raises(beanie_test_db, monkeypatch):
    site = await _connected_site()

    async def _boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(connected_card, "refresh_card_meta", _boom)
    monkeypatch.setattr(screenshot_mod, "take_site_screenshot", _boom)

    await connected_card.refresh_connected_card(site)  # no raise


# --------------------------------------------------------------------------- #
# Triggers
# --------------------------------------------------------------------------- #


async def test_a_first_bind_schedules_the_card_refresh(beanie_test_db, monkeypatch):
    scheduled = _record_scheduled(monkeypatch)
    minted = await _connected_site()

    async def _none(*_a, **_kw):
        return None

    async def _mint(**_kw):
        return minted

    monkeypatch.setattr(sites_service, "foreign_site_for_pocket", _none)
    monkeypatch.setattr(sites_service, "mint_foreign_site", _mint)

    site = await sites_service.bind_foreign_concierge(
        workspace_id="ws1", pocket_id="p1", owner="u1", allowed_origins=[_HOST]
    )

    assert site is minted
    assert len(scheduled) == 1


async def test_a_rebind_schedules_the_card_refresh(beanie_test_db, monkeypatch):
    scheduled = _record_scheduled(monkeypatch)
    site = await _connected_site()

    async def _found(*_a, **_kw):
        return site

    async def _rebind(*_a, **_kw):
        return "agent-2"

    from pocketpaw_ee.paw_bar import agent_provisioning

    monkeypatch.setattr(sites_service, "foreign_site_for_pocket", _found)
    monkeypatch.setattr(agent_provisioning, "rebind_site_agent", _rebind)

    await sites_service.rebind_foreign_concierge(
        workspace_id="ws1", pocket_id=site.pocket_id, agent_id="agent-2", caller_is_admin=True
    )

    assert len(scheduled) == 1


async def test_a_verified_origin_refreshes_the_connected_cards_on_it(beanie_test_db, monkeypatch):
    scheduled = _record_scheduled(monkeypatch)
    await _connected_site()
    await _connected_site(allowed_origins=["other.example"])
    hosted = Site(
        id=ObjectId(),
        workspace="ws1",
        pocket_id="hosted",
        owner="u1",
        script_name="s",
        signed_key="k2",
        allowed_origins=[_HOST],
    )
    await hosted.insert()

    count = await sites_service.schedule_connected_cards_for_origin("ws1", _HOST.upper())

    assert count == 1
    assert len(scheduled) == 1


async def test_the_verify_route_refreshes_connected_cards(beanie_test_db, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites import ownership
    from pocketpaw_ee.sites.router import router as sites_router

    scheduled = _record_scheduled(monkeypatch)
    await _connected_site()

    async def _verified(**_kw):
        now = datetime.now(UTC)
        return SiteOriginClaim(
            workspace="ws1",
            host=_HOST,
            token="t",
            status="verified",
            issued_at=now,
            expires_at=now,
            issued_by="u1",
            method="well-known",
            verified_at=now,
        )

    monkeypatch.setattr(ownership, "verify_origin", _verified)

    class _M:
        workspace = "ws1"
        role = "admin"

    class _U:
        id = "u1"
        active_workspace = "ws1"
        workspaces = [_M()]

    app = FastAPI()
    add_error_handler(app)
    app.include_router(sites_router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id="u1",
            workspace_id="ws1",
            request_id="t",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: _U()
    app.dependency_overrides[require_license] = lambda: None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post("/api/v1/sites/origins/verify", json={"host": _HOST})

    assert resp.status_code == 200, resp.text
    assert len(scheduled) == 1


async def test_the_knowledge_sync_fills_a_missing_title_from_the_harvest(
    beanie_test_db, monkeypatch
):
    """The kb_ingest second chance: reuses the crawl's homepage HTML."""
    from pocketpaw_ee.sites import foreign_grounding, kb_ingest

    calls: list[dict[str, Any]] = []

    def _record(site, **kw):
        calls.append(kw)

    monkeypatch.setattr(connected_card, "schedule_connected_card", _record)
    site = await _connected_site()
    home = "<html><head><title>From Crawl</title></head><body>Words here</body></html>"

    async def _harvest(_site):
        return foreign_grounding.ForeignHarvest(host=_HOST, source={"index.html": home})

    monkeypatch.setattr(foreign_grounding, "harvest_foreign_site", _harvest)
    # Stop at the ingest: this test is about the card hook, not the KB.
    monkeypatch.setattr(kb_ingest, "extract_site_documents", lambda **_kw: [])
    monkeypatch.setattr(kb_ingest, "_schedule_catalog_sync", lambda _site: None)
    monkeypatch.setattr(kb_ingest, "_schedule_first_foreign_screenshot", lambda _site: None)

    report = kb_ingest.SiteKnowledgeReport()
    await kb_ingest._sync_foreign_site_knowledge(site, report, scope="pocket:x", previous=[])

    assert len(calls) == 1
    assert calls[0]["markup"] == home
    assert calls[0]["base_url"] == f"https://{_HOST}/"
    # The screenshot keeps its own first-picture hook; this one is title + icon.
    assert calls[0]["screenshot"] is False


# --------------------------------------------------------------------------- #
# Preview refresh + the wire
# --------------------------------------------------------------------------- #


async def test_preview_refresh_on_a_connected_site_fills_origin_title(
    beanie_test_db, monkeypatch, network, tmp_path
):
    from pathlib import Path

    monkeypatch.setenv("POCKETPAW_UPLOAD_ADAPTER", "local")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    await _verify()
    site = await _connected_site(name="")
    network({(_HOST, "/"): _html("<title>Refreshed Title</title>")})

    class _CF:
        async def capture_screenshot(self, **_kw):
            return _PNG

    monkeypatch.setattr(sites_service, "_cf_client", lambda: _CF())

    out = await sites_service.refresh_site_preview(workspace_id="ws1", site_id=str(site.id))

    assert out.preview_image_url.startswith("/api/v1/uploads/")
    fresh = await Site.get(site.id)
    assert fresh.origin_title == "Refreshed Title"
    assert fresh.name == ""


async def test_an_old_row_serialises_origin_title_empty(beanie_test_db):
    site = await _connected_site()
    await Site.get_pymongo_collection().update_one(
        {"_id": site.id}, {"$unset": {"origin_title": ""}}
    )

    rows = await sites_service.list_for_workspace("ws1")

    assert rows and rows[0].origin_title == ""
    assert rows[0].model_dump()["origin_title"] == ""
