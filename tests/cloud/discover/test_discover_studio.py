# tests/cloud/discover/test_discover_studio.py — studio templates on Discover, over HTTP.
#
# Created 2026-10-02 (feat/studio-templates). The ST-2 slice end to end through the
# real routers: POST /studio-templates public -> the anonymous GET /discover card
# carries absolute media URLs; PATCH private, DELETE and three reports each take
# it off the anonymous index; POST /discover/{id}/use returns the recipe and
# creates nothing.
#
# ``RecordingBus.subscribe`` is a no-op, so ``_sync`` replays the recorded
# studio-template events into the real Discover handler after each write.
from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import rate_limit
from pocketpaw_ee.cloud.discover import listeners
from pocketpaw_ee.cloud.models.studio_generation import StudioGeneration
from pocketpaw_ee.cloud.models.studio_template import StudioTemplate

pytestmark = [pytest.mark.usefixtures("mongo_db"), pytest.mark.asyncio]

WS, OTHER_WS, OWNER = "w1", "w2", "u1"
BASE = "https://paw.example"
SYNCED = {"studio_template.saved", "studio_template.updated", "studio_template.deleted"}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("POCKETPAW_PUBLIC_BASE_URL", BASE)
    rate_limit._discover_public_limiter._buckets.clear()
    rate_limit._discover_report_limiter._buckets.clear()
    yield
    rate_limit._discover_public_limiter._buckets.clear()
    rate_limit._discover_report_limiter._buckets.clear()


@pytest_asyncio.fixture
async def client(recording_bus) -> AsyncClient:
    from fastapi import FastAPI, HTTPException
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.discover.router import router as discover_router
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.studio_templates.router import router as templates_router

    state = SimpleNamespace(user=None)

    async def _user():
        if state.user is None:
            raise HTTPException(401, "Unauthorized")
        return state.user

    def act_as(user_id: str, ws_id: str = OTHER_WS) -> None:
        state.user = SimpleNamespace(id=user_id, active_workspace=ws_id)

    async def sync() -> None:
        events, recording_bus.events[:] = list(recording_bus.events), []
        for event in events:
            if event.type in SYNCED:
                await listeners.on_studio_template_changed(event)

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = _user
    app.include_router(templates_router, prefix="/api/v1")
    app.include_router(discover_router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.act_as = act_as
        c.log_out = lambda: setattr(state, "user", None)
        c.sync = sync
        yield c


async def _publish(client, **body) -> dict:
    await StudioGeneration(
        workspace=WS,
        generation_id="g1",
        prompt="a red fox",
        status="succeeded",
        kind="image",
        model="flux",
        params={
            "kind": "image",
            "model": "flux",
            "aspectRatio": "1:1",
            "count": 1,
            "inputImageCount": 1,
        },
        assets=[{"id": "a1", "url": "/api/v1/media/fox.png", "mime": "image/png"}],
        created_at_ms=1,
    ).insert()
    client.act_as(OWNER, WS)
    resp = await client.post(
        "/api/v1/studio-templates", json={"generation_id": "g1", "title": "Fox", **body}
    )
    assert resp.status_code == 200, resp.text
    await client.sync()
    client.log_out()
    return resp.json()


async def _cards(client) -> list[dict]:
    resp = await client.get("/api/v1/discover", params={"source": "studio_template"})
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


async def test_public_template_is_on_the_anonymous_index_with_absolute_urls(client) -> None:
    await _publish(client, visibility="public")
    [card] = await _cards(client)
    assert (card["kind"], card["title"], card["media_kind"]) == ("image", "Fox", "image")
    assert card["media_url"] == f"{BASE}/api/v1/media/fox.png"
    assert card["preview_image_url"] == f"{BASE}/api/v1/media/fox.png"
    one = await client.get(f"/api/v1/discover/{card['id']}")
    assert one.json()["media_url"] == card["media_url"]


async def test_private_patch_and_delete_take_it_down(client) -> None:
    assert await _cards(client) == []  # nothing yet
    meta = await _publish(client, visibility="public")
    assert len(await _cards(client)) == 1

    client.act_as(OWNER, WS)
    url = f"/api/v1/studio-templates/{meta['id']}"
    assert (await client.patch(url, json={"visibility": "private"})).status_code == 200
    await client.sync()
    client.log_out()
    assert await _cards(client) == []

    client.act_as(OWNER, WS)
    assert (await client.patch(url, json={"visibility": "public"})).status_code == 200
    await client.sync()
    assert len(await _cards(client)) == 1
    client.act_as("u2", WS)
    assert (await client.delete(url)).status_code == 404  # not the owner
    client.act_as(OWNER, WS)
    assert (await client.delete(url)).status_code == 200
    await client.sync()
    client.log_out()
    assert await _cards(client) == []


async def test_three_reports_unlist_and_hide_the_template(client) -> None:
    meta = await _publish(client, visibility="public")
    [card] = await _cards(client)
    for user in ("u3", "u4", "u5"):
        client.act_as(user)
        resp = await client.post(f"/api/v1/discover/{card['id']}/report", json={"reason": "x"})
        assert resp.status_code == 204, resp.text
    await client.sync()
    client.log_out()
    assert await _cards(client) == []
    assert (await StudioTemplate.get(meta["id"])).hidden is True


async def test_use_returns_the_recipe_and_creates_nothing(client) -> None:
    await _publish(client, visibility="public")
    [card] = await _cards(client)
    before = (await StudioGeneration.count(), await StudioTemplate.count())

    client.act_as("u3")
    resp = await client.post(f"/api/v1/discover/{card['id']}/use")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["source"] == "studio_template"
    assert body["result"]["uses_input_images"] is True
    assert body["result"]["recipe"]["prompt"] == "a red fox"
    assert "inputImageCount" not in body["result"]["recipe"]["params"]
    assert (await StudioGeneration.count(), await StudioTemplate.count()) == before
