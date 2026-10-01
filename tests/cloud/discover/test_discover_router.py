# tests/cloud/discover/test_discover_router.py — the /discover HTTP surface (DS-1 part 2).
#
# Created 2026-10-01 (feat/discover-index). Pins: anonymous GET /discover and
# GET /discover/{id} serve exactly the public allow-list, never a hidden listing;
# the query params filter and page (a bad cursor is a CloudError 4xx, not a 500);
# the per-IP 60/min limit; POST /use and /report need sign-in, use counts a remix,
# and three distinct reporters drop the listing from the anonymous index.
#
# Auth runs through the real ``current_user_id`` / ``current_workspace_id``; only
# ``current_active_user`` is swapped for a toggle (the meetings router-test shape).
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import rate_limit
from pocketpaw_ee.cloud.discover import service_admin
from pocketpaw_ee.cloud.models.discover_listing import DiscoverListing
from pocketpaw_ee.cloud.models.pocket import Pocket as PocketDoc
from pocketpaw_ee.cloud.site_templates import service as templates

pytestmark = [pytest.mark.usefixtures("mongo_db"), pytest.mark.asyncio]

WS, OTHER_WS, OWNER = "w1", "w2", "u1"
URL = "/api/v1/discover"
PUBLIC_KEYS = {
    "id",
    "source",
    "kind",
    "title",
    "description",
    "audiences",
    "featured",
    "preview_image_url",
    "live_url",
    "remix_count",
    "created_at",
}


@pytest.fixture(autouse=True)
def _fresh_limiter():
    rate_limit._discover_public_limiter._buckets.clear()
    rate_limit._discover_report_limiter._buckets.clear()
    yield
    rate_limit._discover_public_limiter._buckets.clear()
    rate_limit._discover_report_limiter._buckets.clear()


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    from fastapi import FastAPI, HTTPException
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.discover.router import router
    from pocketpaw_ee.cloud.license import require_license

    state = SimpleNamespace(user=None)

    async def _user():
        if state.user is None:
            raise HTTPException(401, "Unauthorized")
        return state.user

    def act_as(user_id: str, ws_id: str = OTHER_WS) -> None:
        state.user = SimpleNamespace(id=user_id, active_workspace=ws_id)

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = _user
    app.include_router(router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.act_as = act_as
        c.log_out = lambda: setattr(state, "user", None)
        yield c


async def _upsert(source_id: str, **fields: Any) -> str:
    fields.setdefault("workspace", WS)
    fields.setdefault("owner", OWNER)
    fields.setdefault("kind", "site")
    fields.setdefault("title", f"Listing {source_id}")
    return await service_admin.upsert_from_source("site_template", source_id, fields)


async def _template_listing() -> str:
    """A real public site template, listed in Discover."""
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
    return str((await DiscoverListing.find_one({"source_id": meta["id"]})).id)


async def _titles(client: AsyncClient, **params: Any) -> list[str]:
    resp = await client.get(URL, params=params)
    assert resp.status_code == 200, resp.text
    return [item["title"] for item in resp.json()["items"]]


# ---------------------------------------------------------------------------
# Public reads
# ---------------------------------------------------------------------------


async def test_anonymous_list_serves_only_public_unhidden_allow_list(client) -> None:
    shown = await _upsert("a", audiences=["shop"], live_url="https://x")
    hidden = await _upsert("b")
    await service_admin.set_hidden(hidden, True)

    resp = await client.get(URL)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"items", "next_cursor"}
    assert [item["id"] for item in body["items"]] == [shown]
    assert set(body["items"][0]) == PUBLIC_KEYS


async def test_anonymous_get_one_and_hidden_is_404(client) -> None:
    shown = await _upsert("a")
    hidden = await _upsert("b")
    await service_admin.set_hidden(hidden, True)

    ok = await client.get(f"{URL}/{shown}")
    assert ok.status_code == 200
    assert set(ok.json()) == PUBLIC_KEYS
    assert (await client.get(f"{URL}/{hidden}")).status_code == 404
    assert (await client.get(f"{URL}/not-an-id")).status_code == 404


async def test_query_params_filter_and_page(client) -> None:
    await _upsert("a", kind="site", title="Corner Bakery", audiences=["shop"])
    b = await _upsert("b", kind="tool", title="Color tool", audiences=["design"])
    await _upsert("c", kind="game", title="Snake", description="A BAKERY game")
    await service_admin.set_featured(b, True)

    assert await _titles(client, kind="tool") == ["Color tool"]
    assert await _titles(client, audience="shop") == ["Corner Bakery"]
    assert await _titles(client, q="bakery") == ["Snake", "Corner Bakery"]
    assert await _titles(client, featured="true") == ["Color tool"]
    assert await _titles(client, source="other") == []

    first = (await client.get(URL, params={"limit": 2})).json()
    assert [i["title"] for i in first["items"]] == ["Snake", "Color tool"]
    assert first["next_cursor"]
    second = (await client.get(URL, params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert ([i["title"] for i in second["items"]], second["next_cursor"]) == (
        ["Corner Bakery"],
        None,
    )


async def test_bad_query_is_a_4xx_not_a_500(client) -> None:
    bad_cursor = await client.get(URL, params={"cursor": "nope"})
    assert bad_cursor.status_code == 422
    assert bad_cursor.json()["error"]["code"] == "discover.bad_cursor"
    assert (await client.get(URL, params={"limit": 51})).status_code == 422


async def test_public_reads_are_rate_limited_per_ip(client) -> None:
    capacity = rate_limit._discover_public_limiter.capacity
    assert capacity == 60
    ip = {"x-forwarded-for": "203.0.113.7"}

    for i in range(capacity):
        path = URL if i % 2 else f"{URL}/650000000000000000000000"
        assert (await client.get(path, headers=ip)).status_code in (200, 404)
    blocked = await client.get(URL, headers=ip)
    other_ip = await client.get(URL, headers={"x-forwarded-for": "203.0.113.8"})

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "discover.rate_limited"
    assert other_ip.status_code == 200


# ---------------------------------------------------------------------------
# Signed-in use / report
# ---------------------------------------------------------------------------


async def test_use_needs_sign_in_and_counts_a_remix(client) -> None:
    listing_id = await _template_listing()

    assert (await client.post(f"{URL}/{listing_id}/use")).status_code == 401

    client.act_as("u3")
    resp = await client.post(f"{URL}/{listing_id}/use", json={"name": "My bakery"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"source", "result"}
    assert body["source"] == "site_template"
    pocket = await PocketDoc.get(body["result"]["pocket_id"])
    assert (pocket.workspace, pocket.owner, pocket.name) == (OTHER_WS, "u3", "My bakery")
    assert (await DiscoverListing.get(listing_id)).remix_count == 1
    assert (await client.post(f"{URL}/{listing_id}/use")).status_code == 200  # no body


async def test_report_needs_sign_in_and_three_reporters_unlist(client) -> None:
    listing_id = await _upsert("a")
    report = {"reason": "spam"}

    assert (await client.post(f"{URL}/{listing_id}/report", json=report)).status_code == 401

    for user in ("u3", "u4", "u5"):
        client.act_as(user)
        resp = await client.post(f"{URL}/{listing_id}/report", json=report)
        assert resp.status_code == 204, resp.text
        assert resp.content == b""

    client.log_out()
    assert await _titles(client) == []
    assert (await client.get(f"{URL}/{listing_id}")).status_code == 404


async def test_reports_are_rate_limited_per_user(client) -> None:
    listing_id = await _upsert("a")
    report = {"reason": "spam"}
    client.act_as("u3")
    for _ in range(10):  # repeats are no-ops, but each still spends a token
        assert (await client.post(f"{URL}/{listing_id}/report", json=report)).status_code == 204

    blocked = await client.post(f"{URL}/{listing_id}/report", json=report)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "discover.report_rate_limited"

    client.act_as("u4")
    assert (await client.post(f"{URL}/{listing_id}/report", json=report)).status_code == 204
