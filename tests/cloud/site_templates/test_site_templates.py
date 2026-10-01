# tests/cloud/site_templates/test_site_templates.py — user-saved site templates.
#
# ``site_templates.service`` saves a frozen snapshot of a site pocket and later
# starts new site pockets from it through ``pockets.service.copy_site_snapshot``.
# These tests pin: the round trip (save, list, get, use) per field; metadata
# only on every response and event (never snapshot / source / rippleSpec);
# privacy (another user in the workspace, or another workspace, gets NotFound on
# list / get / use / delete); the Sites plan gate on save and use; the size and
# count caps; that deleting a template leaves its pockets alone; the source
# refusals; one audit row per save / use / delete; and the five routes.
# Mutation plan: tests/mutations/site_templates.json.
from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud.models.audit_event import AuditEvent
from pocketpaw_ee.cloud.models.pocket import Pocket as PocketDoc
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.site_template import SiteTemplate
from pocketpaw_ee.cloud.site_templates import service as svc

pytestmark = pytest.mark.usefixtures("mongo_db")

WS = "w1"
OTHER_WS = "w2"
OWNER = "u1"
PEER = "u2"  # same workspace, not the owner

SNAPSHOT_FIELDS = ("engine", "pattern", "rippleSpec", "source", "keeps_client_bundle")
LEAKY_KEYS = {"snapshot", "source", "rippleSpec"}
META_KEYS = {
    "id",
    "name",
    "description",
    "visibility",
    "version",
    "engine",
    "pattern",
    "owner",
    "created_at",
    "updated_at",
}

SVELTE_SOURCE = {
    "src/routes/+page.svelte": "<h1>Bakery</h1>\n",
    "src/routes/+page.ts": "export const prerender = true;\n",
}
RIPPLE_SPEC: dict[str, Any] = {"ui": {"type": "flex", "id": "root", "children": []}}


async def _site(workspace: str = WS, **fields: Any) -> PocketDoc:
    fields.setdefault("type", "site")
    fields.setdefault("owner", OWNER)
    fields.setdefault("name", "Bakery")
    fields.setdefault("engine", "svelte")
    fields.setdefault("pattern", "landing")
    fields.setdefault("source", SVELTE_SOURCE)
    fields.setdefault("rippleSpec", RIPPLE_SPEC)
    fields.setdefault("keeps_client_bundle", True)
    doc = PocketDoc(workspace=workspace, **fields)
    await doc.insert()
    return doc


async def _saved(src: PocketDoc | None = None, user: str = OWNER, **body: Any) -> dict:
    src = src or await _site()
    body.setdefault("name", "Bakery template")
    return await svc.save_template(WS, user, {"pocket_id": str(src.id), **body})


@pytest.fixture(autouse=True)
def sites_plan(monkeypatch) -> dict[str, str]:
    """Synthetic workspaces have no Workspace doc, so answer the real
    ``require_sites_plan`` gate with a Sites plan ("go"). Set ``["plan"]`` to
    "free" to make it deny."""
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    state = {"plan": "go"}

    async def _plan(workspace_id: str) -> str:
        return state["plan"]

    monkeypatch.setattr(workspace_service, "get_workspace_plan", _plan)
    return state


def _assert_meta_only(payload: dict) -> None:
    assert not LEAKY_KEYS & set(payload), payload


def _sans_times(meta: dict) -> dict:
    """Meta without timestamps: Mongo stores milliseconds, so a re-read differs
    from the just-inserted doc in sub-millisecond digits."""
    return {k: v for k, v in meta.items() if k not in {"created_at", "updated_at"}}


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_list_get_use_round_trip() -> None:
    src = await _site(source_gated=True)
    meta = await _saved(src, description="A bakery")

    assert set(meta) == META_KEYS
    assert meta["name"] == "Bakery template" and meta["description"] == "A bakery"
    assert (meta["visibility"], meta["version"], meta["owner"]) == ("private", 1, OWNER)
    assert (meta["engine"], meta["pattern"]) == ("svelte", "landing")

    listed = await svc.list_templates(WS, OWNER)
    assert [_sans_times(t) for t in listed] == [_sans_times(meta)]
    for row in listed:
        _assert_meta_only(row)
    assert _sans_times(await svc.get_template(WS, OWNER, meta["id"])) == _sans_times(meta)

    # The stored snapshot is exactly the five fields plus the source's stamp.
    doc = await SiteTemplate.get(meta["id"])
    assert set(doc.snapshot) == {*SNAPSHOT_FIELDS, "source_gated"}
    assert doc.source_pocket_id == str(src.id)

    used = await svc.use_template(WS, OWNER, meta["id"], {})
    assert set(used) == {"pocket_id"}
    pocket = await PocketDoc.get(used["pocket_id"])
    assert {f: getattr(pocket, f) for f in SNAPSHOT_FIELDS} == {
        f: getattr(src, f) for f in SNAPSHOT_FIELDS
    }
    assert pocket.type == "site" and pocket.owner == OWNER and pocket.workspace == WS
    assert pocket.visibility == "private"
    assert pocket.name == "Bakery template"
    assert (pocket.template_id, pocket.template_version) == (meta["id"], 1)
    assert pocket.source_gated is True
    sites = await Site.find(Site.pocket_id == used["pocket_id"]).to_list()
    assert len(sites) == 1 and sites[0].deployed is False


@pytest.mark.asyncio
async def test_use_takes_a_name() -> None:
    meta = await _saved()
    used = await svc.use_template(WS, OWNER, meta["id"], {"name": "Second bakery"})
    assert (await PocketDoc.get(used["pocket_id"])).name == "Second bakery"


@pytest.mark.asyncio
async def test_template_is_a_frozen_snapshot() -> None:
    src = await _site()
    meta = await _saved(src)
    src.source = {"src/routes/+page.svelte": "<h1>Changed</h1>\n"}
    await src.save()
    used = await svc.use_template(WS, OWNER, meta["id"], {})
    assert (await PocketDoc.get(used["pocket_id"])).source == SVELTE_SOURCE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gate_on", "source_gated", "expected"),
    [(True, False, True), (False, True, True), (False, False, False)],
)
async def test_use_is_never_less_gated_than_a_new_pocket(
    monkeypatch, gate_on: bool, source_gated: bool, expected: bool
) -> None:
    from pocketpaw.config import get_settings

    monkeypatch.setattr(get_settings(), "sites_source_gate_enabled", gate_on)
    meta = await _saved(await _site(source_gated=source_gated))
    used = await svc.use_template(WS, OWNER, meta["id"], {})
    assert (await PocketDoc.get(used["pocket_id"])).source_gated is expected


@pytest.mark.asyncio
async def test_list_is_newest_first() -> None:
    first = await _saved(name="one")
    second = await _saved(name="two")
    assert [t["id"] for t in await svc.list_templates(WS, OWNER)] == [second["id"], first["id"]]


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_excludes_another_users_templates() -> None:
    await _saved()
    assert await svc.list_templates(WS, PEER) == []


@pytest.mark.asyncio
async def test_list_excludes_another_workspaces_templates() -> None:
    await _saved()
    assert await svc.list_templates(OTHER_WS, OWNER) == []


@pytest.mark.asyncio
async def test_peer_cannot_get_or_use() -> None:
    meta = await _saved()
    with pytest.raises(NotFound):
        await svc.get_template(WS, PEER, meta["id"])
    with pytest.raises(NotFound):
        await svc.use_template(WS, PEER, meta["id"], {})
    assert await PocketDoc.find(PocketDoc.owner == PEER).count() == 0


@pytest.mark.asyncio
async def test_peer_cannot_delete() -> None:
    meta = await _saved()
    with pytest.raises(NotFound):
        await svc.delete_template(WS, PEER, meta["id"])
    assert await SiteTemplate.get(meta["id"]) is not None


@pytest.mark.asyncio
async def test_other_workspace_is_not_found() -> None:
    meta = await _saved()
    with pytest.raises(NotFound):
        await svc.get_template(OTHER_WS, OWNER, meta["id"])
    with pytest.raises(NotFound):
        await svc.use_template(OTHER_WS, OWNER, meta["id"], {})
    with pytest.raises(NotFound):
        await svc.delete_template(OTHER_WS, OWNER, meta["id"])
    assert await SiteTemplate.get(meta["id"]) is not None


@pytest.mark.asyncio
async def test_malformed_id_is_not_found() -> None:
    with pytest.raises(NotFound):
        await svc.get_template(WS, OWNER, "not-an-id")


# ---------------------------------------------------------------------------
# Plan gate and caps
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_plan_cannot_save(sites_plan: dict[str, str]) -> None:
    src = await _site()
    sites_plan["plan"] = "free"
    with pytest.raises(Forbidden) as exc:
        await _saved(src)
    assert exc.value.code == "plan.feature_denied"
    assert await SiteTemplate.find_all().count() == 0
    assert await AuditEvent.find_all().count() == 0


@pytest.mark.asyncio
async def test_free_plan_cannot_use(sites_plan: dict[str, str]) -> None:
    meta = await _saved()
    sites_plan["plan"] = "free"
    with pytest.raises(Forbidden) as exc:
        await svc.use_template(WS, OWNER, meta["id"], {})
    assert exc.value.code == "plan.feature_denied"
    assert await PocketDoc.find_all().count() == 1
    assert await Site.find_all().count() == 0


@pytest.mark.asyncio
async def test_oversized_snapshot_is_refused() -> None:
    big = {"src/big.txt": "x" * (svc.MAX_SNAPSHOT_BYTES + 1)}
    with pytest.raises(ValidationError) as exc:
        await _saved(await _site(source=big))
    assert exc.value.code == "site_templates.too_large"
    assert await SiteTemplate.find_all().count() == 0


@pytest.mark.asyncio
async def test_fifty_first_template_is_refused() -> None:
    for i in range(svc.MAX_TEMPLATES_PER_WORKSPACE - 1):
        await SiteTemplate(workspace=WS, owner=PEER, name=f"t{i}", source_pocket_id="p").insert()
    # Another workspace's templates do not count.
    await SiteTemplate(workspace=OTHER_WS, owner=OWNER, name="x", source_pocket_id="p").insert()
    src = await _site()
    await _saved(src)  # the 50th
    with pytest.raises(ValidationError) as exc:
        await _saved(src)
    assert exc.value.code == "site_templates.limit"
    assert await SiteTemplate.find(SiteTemplate.workspace == WS).count() == 50


# ---------------------------------------------------------------------------
# Source refusals, delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_site_pocket_is_refused() -> None:
    with pytest.raises(ValidationError):
        await _saved(await _site(type="custom"))
    assert await SiteTemplate.find_all().count() == 0


@pytest.mark.asyncio
async def test_unreadable_private_pocket_is_refused() -> None:
    src = await _site(visibility="private")
    with pytest.raises(Forbidden):
        await _saved(src, user=PEER)
    assert await SiteTemplate.find_all().count() == 0


@pytest.mark.asyncio
async def test_other_workspace_pocket_is_refused() -> None:
    with pytest.raises(NotFound):
        await _saved(await _site(workspace=OTHER_WS))


@pytest.mark.asyncio
async def test_delete_leaves_used_pockets_intact() -> None:
    meta = await _saved()
    used = await svc.use_template(WS, OWNER, meta["id"], {})
    await svc.delete_template(WS, OWNER, meta["id"])
    assert await SiteTemplate.get(meta["id"]) is None
    pocket = await PocketDoc.get(used["pocket_id"])
    assert pocket is not None and pocket.source == SVELTE_SOURCE
    assert pocket.template_id == meta["id"]
    with pytest.raises(NotFound):
        await svc.get_template(WS, OWNER, meta["id"])


# ---------------------------------------------------------------------------
# Events and audit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_carry_meta_never_the_snapshot(recording_bus) -> None:
    meta = await _saved()
    used = await svc.use_template(WS, OWNER, meta["id"], {})
    await svc.delete_template(WS, OWNER, meta["id"])

    events = [e for e in recording_bus.events if e.type.startswith("site_template.")]
    assert [e.type for e in events] == [
        "site_template.saved",
        "site_template.used",
        "site_template.deleted",
    ]
    for event in events:
        _assert_meta_only(event.data)
        assert event.data["workspace_id"] == WS and event.data["id"] == meta["id"]
        assert event.data["owner"] == OWNER
    assert events[1].data["pocket_id"] == used["pocket_id"]


@pytest.mark.asyncio
async def test_template_events_reach_only_the_owner() -> None:
    from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
    from pocketpaw_ee.cloud._core.realtime.events import SiteTemplateSaved

    resolver = AudienceResolver.__new__(AudienceResolver)
    event = SiteTemplateSaved(data={"owner": OWNER, "workspace_id": WS})
    assert await resolver.audience(event) == [OWNER]


@pytest.mark.asyncio
async def test_one_audit_row_per_save_use_delete() -> None:
    meta = await _saved()
    used = await svc.use_template(WS, OWNER, meta["id"], {})
    await svc.delete_template(WS, OWNER, meta["id"])

    for action, metadata in [
        ("site_template.saved", None),
        ("site_template.used", {"pocket_id": used["pocket_id"]}),
        ("site_template.deleted", {}),
    ]:
        rows = await AuditEvent.find(AuditEvent.action == action).to_list()
        assert len(rows) == 1, action
        row = rows[0]
        assert (row.workspace, row.actor_id, row.target_type, row.target_id) == (
            WS,
            OWNER,
            "site_template",
            meta["id"],
        )
        if metadata is not None:
            assert row.metadata == metadata


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    """The real site-templates router with auth/license pinned to u1 / w1."""
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.deps import current_user_id, current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.site_templates.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_user_id] = lambda: OWNER
    app.dependency_overrides[current_workspace_id] = lambda: WS
    app.dependency_overrides[require_license] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


@pytest.mark.asyncio
async def test_routes_happy_path(client: AsyncClient) -> None:
    src = await _site()
    resp = await client.post(
        "/api/v1/site-templates", json={"pocket_id": str(src.id), "name": "Tpl"}
    )
    assert resp.status_code == 200, resp.text
    meta = resp.json()
    assert set(meta) == META_KEYS
    tid = meta["id"]

    resp = await client.get("/api/v1/site-templates")
    assert resp.status_code == 200, resp.text
    assert [t["id"] for t in resp.json()] == [tid]
    _assert_meta_only(resp.json()[0])

    resp = await client.get(f"/api/v1/site-templates/{tid}")
    assert resp.status_code == 200, resp.text
    assert _sans_times(resp.json()) == _sans_times(meta)

    resp = await client.post(f"/api/v1/site-templates/{tid}/use", json={"name": "New"})
    assert resp.status_code == 200, resp.text
    pocket_id = resp.json()["pocket_id"]
    assert (await PocketDoc.get(pocket_id)).name == "New"

    resp = await client.post(f"/api/v1/site-templates/{tid}/use")
    assert resp.status_code == 200, resp.text

    resp = await client.delete(f"/api/v1/site-templates/{tid}")
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/api/v1/site-templates/{tid}")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_routes_refusals(client: AsyncClient, sites_plan: dict[str, str]) -> None:
    plain = await _site(type="custom")
    resp = await client.post(
        "/api/v1/site-templates", json={"pocket_id": str(plain.id), "name": "Tpl"}
    )
    assert resp.status_code == 422, resp.text

    resp = await client.post("/api/v1/site-templates", json={"pocket_id": "x", "name": ""})
    assert resp.status_code == 422, resp.text

    sites_plan["plan"] = "free"
    src = await _site()
    resp = await client.post(
        "/api/v1/site-templates", json={"pocket_id": str(src.id), "name": "Tpl"}
    )
    assert resp.status_code == 403, resp.text
