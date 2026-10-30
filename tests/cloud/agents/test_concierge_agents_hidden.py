# tests/cloud/agents/test_concierge_agents_hidden.py — site concierges stay off
# the tenant's agent listings.
#
# A site concierge is an agent (the legacy runtime runs one per site, slug
# ``concierge-<site_id>``), but it belongs to the site's concierge page, not to
# /agents. These tests pin that every tenant-facing listing (the /agents list,
# discover, @-mention suggestions, the agents surface snapshot, the planner's
# name matching) leaves concierges out, whether they carry the provisioning
# marker (tags ``concierge`` + ``site:<id>``) or only the legacy slug, and that
# normal agents are still listed. Internal readers that need them keep them:
# get-by-id, get-by-slug and the knowledge aggregation.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.agents import service as agents_service
from pocketpaw_ee.cloud.agents.dto import CreateAgentRequest, DiscoverRequest

pytestmark = pytest.mark.usefixtures("mongo_db")

WS = "w1"
OWNER = "u_owner"
SITE_ID = "a" * 24
LEGACY_SITE_ID = "b" * 24


def _ctx(user_id: str = OWNER) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=WS,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _make(slug: str, *, name: str | None = None, tags: list[str] | None = None) -> str:
    body = CreateAgentRequest(
        name=name or slug.title(),
        slug=slug,
        visibility="workspace",
        soul_enabled=False,
        tags=tags,
    )
    return (await agents_service.create(_ctx(), WS, body)).id


async def _seed() -> dict[str, str]:
    """A normal agent, a marked concierge and a legacy (slug-only) concierge."""
    return {
        "normal": await _make("helper", name="Helper"),
        # Named like the site's concierge would be, carrying the provisioning tags.
        "marked": await _make(
            f"concierge-{SITE_ID}",
            name="Brew Concierge",
            tags=["concierge", f"site:{SITE_ID}"],
        ),
        # An older concierge whose tags were cleared: the slug alone marks it.
        "legacy": await _make(f"concierge-{LEGACY_SITE_ID}", name="Bakery Concierge"),
    }


def _app(viewer: str = OWNER) -> FastAPI:
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.agents.router import router as agents_router
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    app = FastAPI()
    add_error_handler(app)
    app.include_router(agents_router)
    app.dependency_overrides[current_user_id] = lambda: viewer
    app.dependency_overrides[current_workspace_id] = lambda: WS
    app.dependency_overrides[require_license] = lambda: None
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


# --------------------------------------------------------------------------- #
# Tenant-facing listings leave concierges out
# --------------------------------------------------------------------------- #


async def test_agents_list_hides_concierges_from_a_viewer() -> None:
    ids = await _seed()
    async with _client(_app("u_viewer")) as client:
        resp = await client.get("/agents")
    assert resp.status_code == 200
    listed = {a["_id"] for a in resp.json()}
    assert ids["normal"] in listed
    assert ids["marked"] not in listed
    assert ids["legacy"] not in listed


async def test_agents_list_search_hides_concierges() -> None:
    ids = await _seed()
    async with _client(_app()) as client:
        resp = await client.get("/agents", params={"query": "concierge"})
    assert resp.status_code == 200
    assert {a["_id"] for a in resp.json()} == set()
    assert ids  # seeded


async def test_discover_hides_concierges() -> None:
    ids = await _seed()
    async with _client(_app()) as client:
        resp = await client.post("/agents/discover", json={})
    assert resp.status_code == 200
    listed = {a["_id"] for a in resp.json()}
    assert ids["normal"] in listed
    assert not listed & {ids["marked"], ids["legacy"]}

    for visibility in ("workspace", "private"):
        found = await agents_service.discover(_ctx(), WS, DiscoverRequest(visibility=visibility))
        assert {a.id for a in found} == {ids["normal"]}


async def test_mention_suggestions_hide_concierges() -> None:
    ids = await _seed()
    found = {s["id"] for s in await agents_service.suggest_for_mentions(WS, "")}
    assert found == {ids["normal"]}
    assert await agents_service.suggest_for_mentions(WS, "concierge") == []


async def test_service_list_hides_concierges_by_default() -> None:
    ids = await _seed()
    assert {a.id for a in await agents_service.list_agents(WS)} == {ids["normal"]}


async def test_a_user_tag_alone_does_not_hide_an_agent() -> None:
    """Only the provisioning pair (``concierge`` + ``site:<id>``) or the concierge
    slug marks an agent; a member tagging their own agent "concierge" keeps it."""
    mine = await _make("front-desk", tags=["concierge"])
    assert mine in {a.id for a in await agents_service.list_agents(WS)}


def test_is_concierge_agent_reads_the_marker_and_the_slug() -> None:
    from types import SimpleNamespace

    is_concierge = agents_service.is_concierge_agent
    assert is_concierge(SimpleNamespace(slug="x", tags=("concierge", f"site:{SITE_ID}")))
    assert is_concierge(SimpleNamespace(slug=f"concierge-{SITE_ID}", tags=()))
    assert not is_concierge(SimpleNamespace(slug="helper", tags=("concierge",)))
    assert not is_concierge(SimpleNamespace(slug="concierge-desk", tags=()))


# --------------------------------------------------------------------------- #
# Internal readers keep them
# --------------------------------------------------------------------------- #


async def test_direct_reads_still_resolve_a_concierge() -> None:
    ids = await _seed()
    assert (await agents_service.get(ids["marked"])).id == ids["marked"]
    got = await agents_service.get_for_viewer(ids["legacy"], WS, OWNER)
    assert got.id == ids["legacy"]
    by_slug = await agents_service.get_by_slug(WS, f"concierge-{SITE_ID}")
    assert by_slug.id == ids["marked"]
    async with _client(_app()) as client:
        resp = await client.get(f"/agents/{ids['marked']}")
    assert resp.status_code == 200


async def test_internal_listing_can_include_concierges() -> None:
    ids = await _seed()
    every = await agents_service.list_agents(WS, include_concierges=True)
    assert {a.id for a in every} == set(ids.values())


async def test_knowledge_aggregation_still_reads_concierge_scopes() -> None:
    from pocketpaw_ee.cloud.kb.knowledge_router import _list_workspace_agent_ids

    ids = await _seed()
    assert set(await _list_workspace_agent_ids(WS)) == set(ids.values())
