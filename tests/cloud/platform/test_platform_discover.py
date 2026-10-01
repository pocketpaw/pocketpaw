# tests/cloud/platform/test_platform_discover.py — Discover moderation on the platform axis.
#
# Created 2026-10-02 (feat/discover-moderation, DS-5). Pins, over real HTTP with
# the real ``require_platform`` guard (only ``current_active_user`` is swapped):
#   * SUPPORT can GET /platform/discover and gets 403 on every POST; OPERATOR can
#     do all of them; a user with no platform role gets 403 everywhere.
#   * hide drops the listing from the public GET /discover and hides the source
#     template; unhide brings it back, clears reports, records the reporters.
#   * feature shows ``featured: true`` publicly and under ``featured=true``.
#   * reindex returns counts and is idempotent.
#   * every mutation leaves an ``applied`` PlatformAuditEvent; a missing listing
#     404s without one; a blank reason is 422.
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import rate_limit
from pocketpaw_ee.cloud.discover import service, service_admin, sources
from pocketpaw_ee.cloud.models.discover_listing import DiscoverListing
from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
from pocketpaw_ee.cloud.models.pocket import Pocket as PocketDoc
from pocketpaw_ee.cloud.models.site_template import SiteTemplate
from pocketpaw_ee.cloud.site_templates import service as templates

pytestmark = [pytest.mark.usefixtures("mongo_db"), pytest.mark.asyncio]

WS, OTHER_WS, OWNER = "w1", "w2", "u1"
STRANGERS = ("u3", "u4", "u5")
PLATFORM = "/api/v1/platform/discover"
PUBLIC = "/api/v1/discover"
REASON = {"reason": "moderation test"}
VERBS = ("feature", "unfeature", "hide", "unhide")


@pytest.fixture(autouse=True)
def _discover_env(monkeypatch):
    """The Discover suites' autouse fixtures (they live in tests/cloud/discover)."""
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    async def _plan(_workspace_id: str) -> str:
        return "go"

    monkeypatch.setattr(workspace_service, "get_workspace_plan", _plan)
    sources.register_builtin_sources()
    rate_limit._discover_public_limiter._buckets.clear()
    yield
    rate_limit._discover_public_limiter._buckets.clear()


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.discover.router import router as discover_router
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.platform.router import router as platform_router

    state = SimpleNamespace(user=None)

    def act_as(role: str | None) -> None:
        state.user = SimpleNamespace(
            id=f"op-{role}", email=f"{role}@paw.test", platform_role=role, active_workspace=WS
        )

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: state.user
    app.include_router(discover_router, prefix="/api/v1")
    app.include_router(platform_router, prefix="/api/v1")
    # The guard requires the interactive session cookie before it checks the rung.
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t", cookies={"paw_auth": "x"}
    ) as c:
        c.act_as = act_as
        act_as("operator")
        yield c


async def _upsert(source_id: str, **fields: Any) -> str:
    fields.setdefault("workspace", WS)
    fields.setdefault("owner", OWNER)
    fields.setdefault("kind", "site")
    fields.setdefault("title", f"Listing {source_id}")
    return await service_admin.upsert_from_source("site_template", source_id, fields)


async def _template_listing() -> tuple[str, str]:
    """A real public site template listed in Discover: (listing id, template id)."""
    src = PocketDoc(
        workspace=WS,
        type="site",
        owner=OWNER,
        name="Bakery",
        engine="svelte",
        pattern="landing",
        source={"src/routes/+page.svelte": "<h1>Bakery</h1>\n"},
        rippleSpec={"ui": {"type": "flex", "id": "root", "children": []}},
        keeps_client_bundle=True,
    )
    await src.insert()
    meta = await templates.save_template(
        WS, OWNER, {"pocket_id": str(src.id), "name": "Bakery", "visibility": "public"}
    )
    await service_admin.sync_site_template(meta["id"])
    listing = await DiscoverListing.find_one({"source_id": meta["id"]})
    return str(listing.id), meta["id"]


async def _public_ids(client: AsyncClient, **params: Any) -> list[str]:
    resp = await client.get(PUBLIC, params=params)
    assert resp.status_code == 200, resp.text
    return [item["id"] for item in resp.json()["items"]]


# ---------------------------------------------------------------------------
# Rungs
# ---------------------------------------------------------------------------


async def test_support_reads_but_cannot_moderate(client) -> None:
    listing_id = await _upsert("t1")
    client.act_as("support")

    resp = await client.get(PLATFORM)
    assert resp.status_code == 200, resp.text
    assert [i["id"] for i in resp.json()["items"]] == [listing_id]

    for verb in VERBS:
        resp = await client.post(f"{PLATFORM}/{listing_id}/{verb}", json=REASON)
        assert (resp.status_code, resp.json()["detail"]) == (403, "platform.insufficient_role")
    resp = await client.post(f"{PLATFORM}/reindex", json=REASON)
    assert (resp.status_code, resp.json()["detail"]) == (403, "platform.insufficient_role")
    assert await PlatformAuditEvent.find({"action": "platform.discover.moderate"}).count() == 0


async def test_operator_can_do_everything(client) -> None:
    listing_id = await _upsert("t1")
    assert (await client.get(PLATFORM)).status_code == 200
    for verb in VERBS:
        resp = await client.post(f"{PLATFORM}/{listing_id}/{verb}", json=REASON)
        assert resp.status_code == 200, (verb, resp.text)
    assert (await client.post(f"{PLATFORM}/reindex", json=REASON)).status_code == 200


@pytest.mark.parametrize("role", [None, "owner"])
async def test_non_platform_user_gets_403(client, role) -> None:
    listing_id = await _upsert("t1")
    client.act_as(role)
    resp = await client.get(PLATFORM)
    assert (resp.status_code, resp.json()["detail"]) == (403, "platform.not_operator")
    for path in [f"{listing_id}/{verb}" for verb in VERBS] + ["reindex"]:
        resp = await client.post(f"{PLATFORM}/{path}", json=REASON)
        assert (resp.status_code, resp.json()["detail"]) == (403, "platform.not_operator")


# ---------------------------------------------------------------------------
# Staff list
# ---------------------------------------------------------------------------


async def test_staff_list_includes_hidden_with_moderation_fields(client) -> None:
    shown = await _upsert("t1", title="Shown")
    hidden = await _upsert("t2", title="Hidden", workspace=OTHER_WS)
    await service_admin.set_hidden(hidden, True)

    resp = await client.get(PLATFORM)
    items = {i["id"]: i for i in resp.json()["items"]}
    assert set(items) == {shown, hidden}
    row = items[hidden]
    assert (row["hidden"], row["workspace_id"], row["source_id"]) == (True, OTHER_WS, "t2")
    assert {"report_count", "dismissed_reporter_count", "source", "created_at"} <= set(row)

    only_hidden = (await client.get(PLATFORM, params={"hidden": "true"})).json()["items"]
    assert [i["id"] for i in only_hidden] == [hidden]
    only_shown = (await client.get(PLATFORM, params={"hidden": "false", "q": "show"})).json()
    assert [i["id"] for i in only_shown["items"]] == [shown]
    assert await PlatformAuditEvent.find({"action": "platform.discover.read"}).count() == 3


# ---------------------------------------------------------------------------
# Moderation effects
# ---------------------------------------------------------------------------


async def test_hide_then_unhide_reaches_public_list_and_template(client) -> None:
    listing_id, template_id = await _template_listing()
    for user in STRANGERS[:2]:
        await service.report_listing(OTHER_WS, user, listing_id, {"reason": "spam"})

    resp = await client.post(f"{PLATFORM}/{listing_id}/hide", json=REASON)
    assert resp.json()["hidden"] is True
    assert listing_id not in await _public_ids(client)
    assert (await SiteTemplate.get(template_id)).hidden is True

    resp = await client.post(f"{PLATFORM}/{listing_id}/unhide", json=REASON)
    assert resp.json()["hidden"] is False
    assert listing_id in await _public_ids(client)
    assert (await SiteTemplate.get(template_id)).hidden is False
    doc = await DiscoverListing.get(listing_id)
    assert doc.reports == []
    assert doc.dismissed_reporters == sorted(STRANGERS[:2])


async def test_feature_shows_publicly_and_filters(client) -> None:
    featured = await _upsert("t1")
    plain = await _upsert("t2")

    resp = await client.post(f"{PLATFORM}/{featured}/feature", json=REASON)
    assert resp.json()["featured"] is True
    items = {i["id"]: i for i in (await client.get(PUBLIC)).json()["items"]}
    assert (items[featured]["featured"], items[plain]["featured"]) == (True, False)
    assert await _public_ids(client, featured="true") == [featured]


async def test_reindex_returns_counts_and_is_idempotent(client) -> None:
    listing_id, _ = await _template_listing()
    await _upsert("gone")  # no template behind it: reindex removes it

    first = (await client.post(f"{PLATFORM}/reindex", json=REASON)).json()
    assert (first["source"], first["upserted"], first["removed"]) == ("site_template", 1, 1)
    second = (await client.post(f"{PLATFORM}/reindex", json=REASON)).json()
    assert (second["upserted"], second["removed"]) == (1, 0)
    assert await _public_ids(client) == [listing_id]

    bad = await client.post(f"{PLATFORM}/reindex", params={"source": "nope"}, json=REASON)
    assert bad.status_code == 422


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def test_every_mutation_writes_an_applied_audit_row(client) -> None:
    listing_id = await _upsert("t1", workspace=OTHER_WS)
    for verb in VERBS:
        await client.post(f"{PLATFORM}/{listing_id}/{verb}", json=REASON)
    await client.post(f"{PLATFORM}/reindex", json=REASON)

    rows = await PlatformAuditEvent.find({"action": "platform.discover.moderate"}).to_list()
    assert [r.before.get("verb") for r in rows] == [*VERBS, "reindex"]
    assert {r.status for r in rows} == {"applied"}
    assert {r.actor_id for r in rows} == {"op-operator"}
    assert {r.reason for r in rows} == {REASON["reason"]}
    for row in rows[:4]:
        assert (row.target_type, row.target_workspace) == ("discover_listing", OTHER_WS)
        assert row.before["listing_id"] == row.after["listing_id"] == listing_id
    assert rows[4].target_type == "discover_index"
    assert rows[4].after["source"] == "site_template"


async def test_missing_listing_404s_without_an_audit_row(client) -> None:
    resp = await client.post(f"{PLATFORM}/{'0' * 24}/hide", json=REASON)
    assert resp.status_code == 404
    assert await PlatformAuditEvent.find_all().count() == 0


async def test_blank_reason_is_422(client) -> None:
    listing_id = await _upsert("t1")
    resp = await client.post(f"{PLATFORM}/{listing_id}/hide", json={"reason": "  "})
    assert resp.status_code == 422
    assert (await DiscoverListing.get(listing_id)).hidden is False
