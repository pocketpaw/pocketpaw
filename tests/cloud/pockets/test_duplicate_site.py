# tests/cloud/pockets/test_duplicate_site.py — duplicating a site pocket.
#
# ``pockets.service.duplicate_pocket`` copies exactly five authored fields
# (engine, pattern, rippleSpec, source, keeps_client_bundle) plus the
# ``source_gated`` cohort stamp into a new site pocket the caller owns, through
# ``copy_site_snapshot``. These tests pin, per site track, that the copy is
# faithful and independent, that the copy gets its own draft Site row and nothing
# else rides along (no sharing), and the three refusals: cross-tenant (404),
# unreadable private (403), not a site (422). Mutation plan: tests/mutations/pocket_duplicate.json.
from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.models.pocket import Pocket as PocketDoc
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import DuplicatePocketRequest
from pocketpaw_ee.cloud.shared.errors import Forbidden, NotFound, ValidationError

pytestmark = pytest.mark.usefixtures("mongo_db")

WS = "w1"
OTHER_WS = "w2"
OWNER = "u1"
STRANGER = "u2"

SNAPSHOT_FIELDS = ("engine", "pattern", "rippleSpec", "source", "keeps_client_bundle")

SVELTE_SOURCE = {
    "src/routes/+page.svelte": "<h1>Bakery</h1>\n",
    "src/routes/+page.ts": "export const prerender = true;\n",
}
REACT_SOURCE = {
    "src/App.tsx": "export default function App() { return <h1>Hi</h1>; }\n",
    "paw.dependencies.json": '{"dependencies": {"gsap": "3.12.5"}}',
}
DYNAMIC_SOURCE: dict[str, Any] = {
    "src/routes/+page.svelte": "<ul>{#each rows as r}<li>{r.name}</li>{/each}</ul>\n",
    "objects": [{"name": "guest", "fields": [{"name": "name", "type": "text"}]}],
    "sources": [{"id": "guests", "object": "guest"}],
    "actions": [{"id": "sign", "object": "guest", "op": "insert"}],
    "auth": False,
}
RIPPLE_SPEC: dict[str, Any] = {
    "ui": {"type": "flex", "id": "root", "children": [{"type": "text", "id": "t1"}]},
    "state": {"n": 1},
}

SITES = {
    "svelte": dict(engine="svelte", pattern="landing", source=SVELTE_SOURCE),
    "react": dict(engine="react", pattern="landing", source=REACT_SOURCE, keeps_client_bundle=True),
    "dynamic": dict(engine="svelte", pattern="dynamic", source=DYNAMIC_SOURCE),
    "ripple": dict(engine="ripple", pattern="landing", rippleSpec=RIPPLE_SPEC),
}


async def _site(workspace: str = WS, **fields: Any) -> PocketDoc:
    fields.setdefault("type", "site")
    fields.setdefault("owner", OWNER)
    fields.setdefault("name", "Bakery")
    doc = PocketDoc(workspace=workspace, **fields)
    await doc.insert()
    return doc


def _snapshot(doc: PocketDoc) -> dict[str, Any]:
    return {f: getattr(doc, f) for f in SNAPSHOT_FIELDS}


@pytest.mark.asyncio
@pytest.mark.parametrize("track", sorted(SITES))
async def test_copy_is_faithful_per_track(track: str) -> None:
    src = await _site(
        source_gated=True,
        shared_with=["u9"],
        team=["u8"],
        tool_specs=[{"id": "t"}],
        share_link_token="tok",
        **SITES[track],
    )

    wire = await pockets_service.duplicate_pocket(WS, OWNER, str(src.id), {})

    assert wire["_id"] != str(src.id)
    copy = await PocketDoc.get(wire["_id"])
    assert copy is not None
    assert _snapshot(copy) == _snapshot(src)
    assert copy.type == "site"
    assert copy.source_gated is True
    assert copy.name == "Bakery (copy)"
    assert copy.owner == OWNER
    assert copy.workspace == WS
    assert copy.template_id is None and copy.template_version is None
    # Nothing outside the snapshot rides along.
    assert copy.shared_with == [] and copy.team == [] and copy.tool_specs == []
    assert copy.share_link_token is None
    # The copy lists in the gallery through a fresh DRAFT Site of its own.
    sites = await Site.find_all().to_list()
    assert len(sites) == 1
    assert sites[0].pocket_id == wire["_id"]
    assert sites[0].deployed is False


@pytest.mark.asyncio
async def test_source_gated_false_is_inherited_too() -> None:
    src = await _site(source_gated=False, **SITES["svelte"])
    wire = await pockets_service.duplicate_pocket(WS, OWNER, str(src.id), {})
    assert (await PocketDoc.get(wire["_id"])).source_gated is False


@pytest.mark.asyncio
async def test_copy_is_deep() -> None:
    src = await _site(**SITES["dynamic"])
    wire = await pockets_service.duplicate_pocket(
        WS, OWNER, str(src.id), DuplicatePocketRequest(name="Guestbook v2")
    )
    copy = await PocketDoc.get(wire["_id"])
    assert copy.name == "Guestbook v2"

    copy.source["objects"][0]["name"] = "changed"
    copy.source["src/routes/+page.svelte"] = "gone"
    await copy.save()

    original = await PocketDoc.get(src.id)
    assert original.source == DYNAMIC_SOURCE


@pytest.mark.asyncio
async def test_copy_site_snapshot_does_not_alias_its_input() -> None:
    # Nested on purpose: pydantic re-builds the top-level dict on construction, so
    # only a nested value can show whether the snapshot was deep-copied.
    snap = {"engine": "svelte", "pattern": "dynamic", "source": {"objects": [{"name": "guest"}]}}
    wire = await pockets_service.copy_site_snapshot(
        snap, workspace_id=WS, owner=OWNER, name="T", template_id="tpl", template_version=3
    )
    snap["source"]["objects"][0]["name"] = "mutated"
    assert wire["source"] == {"objects": [{"name": "guest"}]}
    copy = await PocketDoc.get(wire["_id"])
    assert copy.source == {"objects": [{"name": "guest"}]}
    assert (copy.template_id, copy.template_version) == ("tpl", 3)


@pytest.mark.asyncio
async def test_emits_pocket_created(recording_bus) -> None:
    src = await _site(**SITES["svelte"])
    wire = await pockets_service.duplicate_pocket(WS, OWNER, str(src.id), {})
    created = [e for e in recording_bus.events if e.type == "pocket.created"]
    assert [e.data["pocket_id"] for e in created] == [wire["_id"]]


@pytest.mark.asyncio
async def test_cross_workspace_is_not_found() -> None:
    # Workspace-visible and owned by the caller: only the tenant filter stops it.
    src = await _site(workspace=OTHER_WS, **SITES["svelte"])
    with pytest.raises(NotFound):
        await pockets_service.duplicate_pocket(WS, OWNER, str(src.id), {})
    assert await PocketDoc.find(PocketDoc.workspace == WS).count() == 0


@pytest.mark.asyncio
async def test_malformed_id_is_not_found() -> None:
    with pytest.raises(NotFound):
        await pockets_service.duplicate_pocket(WS, OWNER, "not-an-id", {})


@pytest.mark.asyncio
async def test_unreadable_private_pocket_is_forbidden() -> None:
    src = await _site(visibility="private", **SITES["svelte"])
    with pytest.raises(Forbidden):
        await pockets_service.duplicate_pocket(WS, STRANGER, str(src.id), {})
    assert await PocketDoc.find_all().count() == 1


@pytest.mark.asyncio
async def test_shared_private_pocket_is_readable() -> None:
    src = await _site(visibility="private", shared_with=[STRANGER], **SITES["svelte"])
    wire = await pockets_service.duplicate_pocket(WS, STRANGER, str(src.id), {})
    assert wire["owner"] == STRANGER


@pytest.mark.asyncio
async def test_non_site_pocket_is_rejected() -> None:
    src = await _site(type="custom", rippleSpec=RIPPLE_SPEC)
    with pytest.raises(ValidationError):
        await pockets_service.duplicate_pocket(WS, OWNER, str(src.id), {})


@pytest_asyncio.fixture
async def pockets_client() -> AsyncClient:
    """The real pockets router with auth/license pinned to u1 / w1."""
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.pockets.router import router as pockets_router
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    app = FastAPI()
    add_error_handler(app)
    app.include_router(pockets_router, prefix="/api/v1")
    app.dependency_overrides[current_user_id] = lambda: OWNER
    app.dependency_overrides[current_workspace_id] = lambda: WS
    app.dependency_overrides[require_license] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        yield client


@pytest.mark.asyncio
async def test_route_duplicates(pockets_client: AsyncClient) -> None:
    src = await _site(**SITES["react"])

    resp = await pockets_client.post(f"/api/v1/pockets/{src.id}/duplicate", json={"name": "B2"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "B2" and body["type"] == "site" and body["_id"] != str(src.id)

    resp = await pockets_client.post(f"/api/v1/pockets/{src.id}/duplicate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Bakery (copy)"


@pytest.mark.asyncio
async def test_route_refusals(pockets_client: AsyncClient) -> None:
    other = await _site(workspace=OTHER_WS, **SITES["svelte"])
    resp = await pockets_client.post(f"/api/v1/pockets/{other.id}/duplicate")
    assert resp.status_code == 404, resp.text

    plain = await _site(type="custom")
    resp = await pockets_client.post(f"/api/v1/pockets/{plain.id}/duplicate")
    assert resp.status_code == 422, resp.text
