# tests/cloud/site_templates/test_site_templates.py — user-saved site templates.
#
# ``site_templates.service`` saves a frozen snapshot of a site pocket and later
# starts new site pockets from it through ``pockets.service.copy_site_snapshot``.
# These tests pin: the round trip (save, list, get, use) per field; metadata
# only on every response and event (never snapshot / source / rippleSpec);
# the visibility matrix (owner / same-workspace member / other-workspace user x
# private / workspace / public x list scopes / get / use / patch / delete), with
# NotFound for everything a caller may not see; that a public use lands in the
# CALLER's workspace under the caller's plan and cap; no owner id for
# non-owners; the publish checks (private assets, gated source) on save and on
# PATCH; reports (one per user, owner refused, the third distinct reporter
# hides); pagination; the Sites plan gate, size and count caps; that deleting a
# template leaves its pockets alone; the source refusals; audit rows; and the
# routes. The asset detector's per-shape tests: test_private_assets.py.
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
from pydantic import ValidationError as PydanticValidationError

pytestmark = pytest.mark.usefixtures("mongo_db")

WS = "w1"
OTHER_WS = "w2"
OWNER = "u1"
PEER = "u2"  # same workspace, not the owner
STRANGER = "u3"  # another workspace

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
    "is_mine",
    "hidden",
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
    "free" to make it deny everywhere, or ``[<workspace id>]`` for one workspace."""
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    state = {"plan": "go"}

    async def _plan(workspace_id: str) -> str:
        return state.get(workspace_id, state["plan"])

    monkeypatch.setattr(workspace_service, "get_workspace_plan", _plan)
    return state


async def _listed(workspace: str, user: str, **query: Any) -> list[dict]:
    return (await svc.list_templates(workspace, user, query))["templates"]


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

    listed = await _listed(WS, OWNER)
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
    assert [t["id"] for t in await _listed(WS, OWNER)] == [second["id"], first["id"]]


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_excludes_another_users_templates() -> None:
    await _saved()
    assert await _listed(WS, PEER) == []


@pytest.mark.asyncio
async def test_list_excludes_another_workspaces_templates() -> None:
    await _saved()
    assert await _listed(OTHER_WS, OWNER) == []


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
    event = SiteTemplateSaved(data={"user_id": OWNER, "owner": None, "workspace_id": WS})
    assert await resolver.audience(event) == [OWNER]


@pytest.mark.asyncio
async def test_one_audit_row_per_save_use_delete() -> None:
    meta = await _saved()
    used = await svc.use_template(WS, OWNER, meta["id"], {})
    await svc.delete_template(WS, OWNER, meta["id"])

    for action, metadata in [
        ("site_template.saved", None),
        ("site_template.used", {"pocket_id": used["pocket_id"], "template_workspace_id": WS}),
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


@pytest.fixture
def who() -> dict[str, str]:
    """Who the ``client`` fixture acts as; tests may switch it mid-test."""
    return {"user": OWNER, "ws": WS}


@pytest_asyncio.fixture
async def client(who: dict[str, str]) -> AsyncClient:
    """The real site-templates router with auth/license pinned to ``who``."""
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.deps import current_user_id, current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.site_templates.router import router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[current_user_id] = lambda: who["user"]
    app.dependency_overrides[current_workspace_id] = lambda: who["ws"]
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
    assert [t["id"] for t in resp.json()["templates"]] == [tid]
    _assert_meta_only(resp.json()["templates"][0])

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


# ---------------------------------------------------------------------------
# Sharing: visibility matrix
# ---------------------------------------------------------------------------

# (user, workspace) for each kind of caller.
ACTORS = {"owner": (OWNER, WS), "member": (PEER, WS), "stranger": (STRANGER, OTHER_WS)}

# Who may read (get / use) a template of each visibility.
CAN_READ = {
    "private": {"owner"},
    "workspace": {"owner", "member"},
    "public": {"owner", "member", "stranger"},
}


def _ids(rows: list[dict]) -> list[str]:
    return [r["id"] for r in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", ["private", "workspace", "public"])
@pytest.mark.parametrize("actor", ["owner", "member", "stranger"])
async def test_visibility_matrix(visibility: str, actor: str) -> None:
    meta = await _saved(visibility=visibility)
    user, ws = ACTORS[actor]
    can_read = actor in CAN_READ[visibility]

    # list, each scope
    assert _ids(await _listed(ws, user, scope="mine")) == ([meta["id"]] if actor == "owner" else [])
    in_workspace_scope = visibility == "workspace" and actor != "stranger"
    assert _ids(await _listed(ws, user, scope="workspace")) == (
        [meta["id"]] if in_workspace_scope else []
    )
    assert _ids(await _listed(ws, user, scope="public")) == (
        [meta["id"]] if visibility == "public" else []
    )

    # get / use
    if can_read:
        assert (await svc.get_template(ws, user, meta["id"]))["id"] == meta["id"]
        used = await svc.use_template(ws, user, meta["id"], {})
        assert (await PocketDoc.get(used["pocket_id"])).workspace == ws
    else:
        with pytest.raises(NotFound):
            await svc.get_template(ws, user, meta["id"])
        with pytest.raises(NotFound):
            await svc.use_template(ws, user, meta["id"], {})
        assert await PocketDoc.find(PocketDoc.owner == user).count() == 0

    # patch / delete: owner only
    if actor == "owner":
        assert (await svc.update_template(ws, user, meta["id"], {"name": "N"}))["name"] == "N"
        await svc.delete_template(ws, user, meta["id"])
        assert await SiteTemplate.get(meta["id"]) is None
    else:
        with pytest.raises(NotFound):
            await svc.update_template(ws, user, meta["id"], {"name": "N"})
        with pytest.raises(NotFound):
            await svc.delete_template(ws, user, meta["id"])
        doc = await SiteTemplate.get(meta["id"])
        assert doc is not None and doc.name == "Bakery template"


@pytest.mark.asyncio
async def test_owner_is_hidden_from_non_owners() -> None:
    public = await _saved(visibility="public")
    shared = await _saved(visibility="workspace")
    assert (public["owner"], public["is_mine"]) == (OWNER, True)

    seen = [
        await svc.get_template(OTHER_WS, STRANGER, public["id"]),
        *(await _listed(OTHER_WS, STRANGER, scope="public")),
        await svc.get_template(WS, PEER, shared["id"]),
        *(await _listed(WS, PEER, scope="workspace")),
    ]
    assert len(seen) == 4
    for meta in seen:
        assert (meta["owner"], meta["is_mine"]) == (None, False), meta
        assert OWNER not in meta.values() and WS not in meta.values(), meta
        assert set(meta) == META_KEYS
        _assert_meta_only(meta)


@pytest.mark.asyncio
async def test_mine_lists_every_visibility() -> None:
    ids = {(await _saved(visibility=v))["id"] for v in ("private", "workspace", "public")}
    assert set(_ids(await _listed(WS, OWNER))) == ids


# ---------------------------------------------------------------------------
# Sharing: using a public template from another workspace
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_use_lands_in_the_callers_workspace(recording_bus) -> None:
    meta = await _saved(visibility="public")
    used = await svc.use_template(OTHER_WS, STRANGER, meta["id"], {})

    pocket = await PocketDoc.get(used["pocket_id"])
    assert (pocket.workspace, pocket.owner, pocket.visibility) == (OTHER_WS, STRANGER, "private")
    assert (pocket.template_id, pocket.source) == (meta["id"], SVELTE_SOURCE)
    site = await Site.find_one(Site.pocket_id == used["pocket_id"])
    assert site is not None and site.workspace == OTHER_WS

    rows = await AuditEvent.find(AuditEvent.action == "site_template.used").to_list()
    assert len(rows) == 1
    assert (rows[0].workspace, rows[0].actor_id, rows[0].target_id) == (
        OTHER_WS,
        STRANGER,
        meta["id"],
    )
    assert rows[0].metadata == {"pocket_id": used["pocket_id"]}

    [event] = [e for e in recording_bus.events if e.type == "site_template.used"]
    assert event.data["user_id"] == STRANGER and event.data["workspace_id"] == OTHER_WS
    assert event.data["owner"] is None
    _assert_meta_only(event.data)


@pytest.mark.asyncio
async def test_public_use_obeys_the_callers_plan(sites_plan: dict[str, str]) -> None:
    meta = await _saved(visibility="public")
    sites_plan[OTHER_WS] = "free"
    with pytest.raises(Forbidden) as exc:
        await svc.use_template(OTHER_WS, STRANGER, meta["id"], {})
    assert exc.value.code == "plan.feature_denied"
    assert await PocketDoc.find(PocketDoc.workspace == OTHER_WS).count() == 0
    # The owner's workspace still has Sites.
    await svc.use_template(WS, OWNER, meta["id"], {})


@pytest.mark.asyncio
async def test_public_use_obeys_the_callers_pocket_cap(monkeypatch) -> None:
    from pocketpaw_ee.cloud._core.errors import PocketLimitError
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    async def _cap(workspace_id: str) -> tuple[bool, int, int | None]:
        return (workspace_id == OTHER_WS, 3, 3)

    monkeypatch.setattr(pockets_service, "_pocket_cap_exceeded", _cap)
    meta = await _saved(visibility="public")
    with pytest.raises(PocketLimitError):
        await svc.use_template(OTHER_WS, STRANGER, meta["id"], {})
    assert await PocketDoc.find(PocketDoc.workspace == OTHER_WS).count() == 0
    await svc.use_template(WS, OWNER, meta["id"], {})


# ---------------------------------------------------------------------------
# Sharing: PATCH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_changes_metadata_not_version(recording_bus) -> None:
    meta = await _saved()
    out = await svc.update_template(
        WS, OWNER, meta["id"], {"name": "New", "description": "D", "visibility": "workspace"}
    )
    assert (out["name"], out["description"], out["visibility"], out["version"]) == (
        "New",
        "D",
        "workspace",
        1,
    )
    doc = await SiteTemplate.get(meta["id"])
    assert (doc.name, doc.description, doc.visibility, doc.version) == ("New", "D", "workspace", 1)
    assert _ids(await _listed(WS, PEER, scope="workspace")) == [meta["id"]]

    [event] = [e for e in recording_bus.events if e.type == "site_template.updated"]
    assert event.data["user_id"] == OWNER and event.data["visibility"] == "workspace"
    _assert_meta_only(event.data)
    rows = await AuditEvent.find(AuditEvent.action == "site_template.updated").to_list()
    assert len(rows) == 1 and rows[0].metadata == {"visibility": "workspace"}


@pytest.fixture
def source_gate(monkeypatch) -> dict[str, bool]:
    """SF-2 on, and the workspace NOT entitled to read gated source unless
    ``["entitled"]`` is set."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    from pocketpaw.config import get_settings

    state = {"entitled": False}

    async def _entitled(workspace_id: str) -> bool:
        return state["entitled"]

    monkeypatch.setattr(get_settings(), "sites_source_gate_enabled", True)
    monkeypatch.setattr(pockets_service, "_workspace_source_entitled", _entitled)
    return state


@pytest.mark.asyncio
async def test_patch_to_public_refuses_private_assets() -> None:
    src = await _site(source={"src/routes/+page.svelte": '<img src="/api/v1/uploads/abc123">'})
    meta = await _saved(src)
    with pytest.raises(ValidationError) as exc:
        await svc.update_template(WS, OWNER, meta["id"], {"visibility": "public"})
    assert exc.value.code == "site_templates.private_assets"
    assert (await SiteTemplate.get(meta["id"])).visibility == "private"
    # Workspace sharing does not run the publish checks.
    await svc.update_template(WS, OWNER, meta["id"], {"visibility": "workspace"})


@pytest.mark.asyncio
async def test_patch_to_public_refuses_gated_source(source_gate: dict[str, bool]) -> None:
    meta = await _saved(await _site(source_gated=True))
    with pytest.raises(Forbidden) as exc:
        await svc.update_template(WS, OWNER, meta["id"], {"visibility": "public"})
    assert exc.value.code == "site_templates.source_not_shareable"
    assert (await SiteTemplate.get(meta["id"])).visibility == "private"


@pytest.mark.asyncio
async def test_patch_to_public_runs_the_plan_gate(sites_plan: dict[str, str]) -> None:
    meta = await _saved()
    sites_plan["plan"] = "free"
    with pytest.raises(Forbidden) as exc:
        await svc.update_template(WS, OWNER, meta["id"], {"visibility": "public"})
    assert exc.value.code == "plan.feature_denied"
    assert (await SiteTemplate.get(meta["id"])).visibility == "private"


# ---------------------------------------------------------------------------
# Sharing: publish checks on save
# ---------------------------------------------------------------------------

# One sample per URL shape the platform mints for workspace files.
PRIVATE_ASSET_SAMPLES = {
    "uploads_api": "/api/v1/uploads/abc123",
    "files_api": "/api/v1/files/content?path=a.png",
    "media_api": "/api/v1/media/1759300000000-abcdef012345.png",
    "avatar_api": "/api/v1/auth/avatar/u1.png",
    "uploads_mount": "/uploads/avatars/u1.png",
    "storage_key": "chat/202609/0123456789abcdef0123456789abcdef.png",
    "presigned": "https://b.s3.amazonaws.com/k.png?X-Amz-Expires=60&X-Amz-Signature=abc",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(PRIVATE_ASSET_SAMPLES))
async def test_publish_refuses_a_private_asset(shape: str) -> None:
    url = PRIVATE_ASSET_SAMPLES[shape]
    spec = {"ui": {"type": "image", "id": "hero", "props": {"src": url}}}
    with pytest.raises(ValidationError) as exc:
        await _saved(await _site(rippleSpec=spec), visibility="public")
    assert exc.value.code == "site_templates.private_assets"
    assert await SiteTemplate.find_all().count() == 0
    # The same site may still be saved privately.
    await _saved(await _site(rippleSpec=spec))


@pytest.mark.asyncio
async def test_publish_refusal_counts_the_references() -> None:
    source = {
        "src/routes/+page.svelte": '<img src="/api/v1/uploads/a1"><img src="/api/v1/uploads/b2">',
        "src/lib/x.ts": 'const a = "/api/v1/uploads/a1";',
    }
    with pytest.raises(ValidationError) as exc:
        await _saved(await _site(source=source), visibility="public")
    assert "2 private" in exc.value.message


@pytest.mark.asyncio
async def test_publish_allows_external_images() -> None:
    source = {
        "src/routes/+page.svelte": (
            '<img src="https://images.unsplash.com/photo-1?w=800&q=80">'
            '<img src="https://blog.example.com/wp-content/uploads/2024/01/x.jpg">'
        )
    }
    meta = await _saved(await _site(source=source), visibility="public")
    assert meta["visibility"] == "public"


@pytest.mark.asyncio
async def test_publish_refuses_gated_source(source_gate: dict[str, bool]) -> None:
    src = await _site(source_gated=True)
    with pytest.raises(Forbidden) as exc:
        await _saved(src, visibility="public")
    assert exc.value.code == "site_templates.source_not_shareable"
    assert await SiteTemplate.find_all().count() == 0
    # Workspace sharing stays inside the workspace: no check.
    await _saved(src, visibility="workspace")
    # An entitled workspace may publish it.
    source_gate["entitled"] = True
    assert (await _saved(src, visibility="public"))["visibility"] == "public"


@pytest.mark.asyncio
async def test_publish_allows_ungated_source(source_gate: dict[str, bool]) -> None:
    meta = await _saved(await _site(source_gated=False), visibility="public")
    assert meta["visibility"] == "public"


# ---------------------------------------------------------------------------
# Sharing: reports and hiding
# ---------------------------------------------------------------------------


async def _report(template_id: str, user: str, ws: str = OTHER_WS) -> dict:
    return await svc.report_template(ws, user, template_id, {"reason": "spam"})


@pytest.mark.asyncio
async def test_report_is_idempotent_per_user() -> None:
    meta = await _saved(visibility="public")
    assert await _report(meta["id"], STRANGER) == {"id": meta["id"], "reported": True}
    assert await _report(meta["id"], STRANGER) == {"id": meta["id"], "reported": True}
    doc = await SiteTemplate.get(meta["id"])
    assert [r["user"] for r in doc.reports] == [STRANGER]
    assert doc.reports[0]["reason"] == "spam"
    rows = await AuditEvent.find(AuditEvent.action == "site_template.reported").to_list()
    assert len(rows) == 1
    assert (rows[0].workspace, rows[0].actor_id) == (OTHER_WS, STRANGER)


@pytest.mark.asyncio
async def test_owner_cannot_report() -> None:
    meta = await _saved(visibility="public")
    with pytest.raises(Forbidden) as exc:
        await _report(meta["id"], OWNER, WS)
    assert exc.value.code == "site_templates.own_template"
    assert (await SiteTemplate.get(meta["id"])).reports == []


@pytest.mark.asyncio
async def test_reporting_a_template_you_cannot_see_is_not_found() -> None:
    private = await _saved(visibility="private")
    shared = await _saved(visibility="workspace")
    with pytest.raises(NotFound):
        await _report(private["id"], STRANGER)
    with pytest.raises(NotFound):
        await _report(shared["id"], STRANGER)
    # Reports are for public templates only, even for a member who can see it.
    with pytest.raises(NotFound):
        await _report(shared["id"], PEER, WS)


@pytest.mark.asyncio
async def test_duplicate_reports_do_not_reach_the_threshold() -> None:
    meta = await _saved(visibility="public")
    await _report(meta["id"], STRANGER)
    await _report(meta["id"], STRANGER)
    await _report(meta["id"], STRANGER)
    await _report(meta["id"], "u4")
    assert (await SiteTemplate.get(meta["id"])).hidden is False
    assert _ids(await _listed(OTHER_WS, STRANGER, scope="public")) == [meta["id"]]


@pytest.mark.asyncio
async def test_third_distinct_report_hides(recording_bus) -> None:
    meta = await _saved(visibility="public")
    await _report(meta["id"], STRANGER)
    await _report(meta["id"], PEER, WS)
    assert (await SiteTemplate.get(meta["id"])).hidden is False
    await _report(meta["id"], "u4", "w3")
    assert (await SiteTemplate.get(meta["id"])).hidden is True

    # Gone for every non-owner: public list, get, use.
    for user, ws in [(STRANGER, OTHER_WS), (PEER, WS)]:
        assert await _listed(ws, user, scope="public") == []
        with pytest.raises(NotFound):
            await svc.get_template(ws, user, meta["id"])
        with pytest.raises(NotFound):
            await svc.use_template(ws, user, meta["id"], {})
    assert await PocketDoc.find(PocketDoc.owner != OWNER).count() == 0

    # The owner still sees it, flagged hidden, and can still use it.
    assert (await svc.get_template(WS, OWNER, meta["id"]))["hidden"] is True
    assert [(t["id"], t["hidden"]) for t in await _listed(WS, OWNER)] == [(meta["id"], True)]
    await svc.use_template(WS, OWNER, meta["id"], {})

    rows = await AuditEvent.find(AuditEvent.action == "site_template.hidden").to_list()
    assert len(rows) == 1
    assert (rows[0].workspace, rows[0].actor_id) == (WS, "system")
    [event] = [
        e for e in recording_bus.events if e.type == "site_template.updated" and e.data["hidden"]
    ]
    assert event.data["user_id"] == OWNER
    _assert_meta_only(event.data)


@pytest.mark.asyncio
async def test_hidden_flag_is_shown_to_the_owner_only() -> None:
    meta = await _saved(visibility="public")
    for user, ws in [(STRANGER, OTHER_WS), (PEER, WS), ("u4", "w3")]:
        await _report(meta["id"], user, ws)
    # A hidden template the owner moves back to workspace sharing is visible to
    # members again, without the moderation flag.
    await svc.update_template(WS, OWNER, meta["id"], {"visibility": "workspace"})
    assert (await svc.get_template(WS, PEER, meta["id"]))["hidden"] is False
    assert [t["hidden"] for t in await _listed(WS, PEER, scope="workspace")] == [False]
    assert (await svc.get_template(WS, OWNER, meta["id"]))["hidden"] is True


@pytest.mark.asyncio
async def test_reports_stop_at_the_cap(monkeypatch) -> None:
    # Keep it listed while the reports pile up: a hidden template is NotFound.
    monkeypatch.setattr(svc, "HIDE_THRESHOLD", 10_000)
    meta = await _saved(visibility="public")
    for i in range(svc.MAX_REPORTS + 2):
        await _report(meta["id"], f"r{i}")
    assert len((await SiteTemplate.get(meta["id"])).reports) == svc.MAX_REPORTS


# ---------------------------------------------------------------------------
# Sharing: pagination
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_public_list_paginates_newest_first() -> None:
    ids = []
    for i in range(5):
        doc = SiteTemplate(
            workspace=f"w{i}",
            owner=f"o{i}",
            name=f"t{i}",
            source_pocket_id="p",
            visibility="public",
        )
        await doc.insert()
        ids.append(str(doc.id))
    await SiteTemplate(workspace=WS, owner=OWNER, name="private", source_pocket_id="p").insert()
    await SiteTemplate(
        workspace="w9",
        owner="o9",
        name="hid",
        source_pocket_id="p",
        visibility="public",
        hidden=True,
    ).insert()

    pages, cursor = [], None
    # Bounded: a cursor that is ignored would page forever.
    for _ in range(5):
        page = await svc.list_templates(
            OTHER_WS, STRANGER, {"scope": "public", "limit": 2, "cursor": cursor}
        )
        pages.append(_ids(page["templates"]))
        cursor = page["next_cursor"]
        if cursor is None:
            break
    newest_first = ids[::-1]
    assert pages == [newest_first[:2], newest_first[2:4], newest_first[4:]]


@pytest.mark.asyncio
async def test_list_refuses_a_bad_cursor_or_limit() -> None:
    with pytest.raises(ValidationError):
        await svc.list_templates(WS, OWNER, {"scope": "public", "cursor": "nope"})
    with pytest.raises(PydanticValidationError):
        await svc.list_templates(WS, OWNER, {"scope": "public", "limit": 51})


# ---------------------------------------------------------------------------
# Sharing: routes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routes_sharing(client: AsyncClient, who: dict[str, str]) -> None:
    src = await _site()
    resp = await client.post(
        "/api/v1/site-templates",
        json={"pocket_id": str(src.id), "name": "Tpl", "visibility": "public"},
    )
    assert resp.status_code == 200, resp.text
    tid = resp.json()["id"]

    who.update(user=STRANGER, ws=OTHER_WS)
    resp = await client.get("/api/v1/site-templates", params={"scope": "public", "limit": 1})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert _ids(body["templates"]) == [tid] and body["next_cursor"] is None
    assert body["templates"][0]["owner"] is None
    _assert_meta_only(body["templates"][0])

    resp = await client.get(f"/api/v1/site-templates/{tid}")
    assert resp.status_code == 200, resp.text
    resp = await client.patch(f"/api/v1/site-templates/{tid}", json={"name": "Mine now"})
    assert resp.status_code == 404, resp.text
    for _ in range(2):
        resp = await client.post(f"/api/v1/site-templates/{tid}/report", json={"reason": "spam"})
        assert resp.status_code == 200, resp.text
    resp = await client.post(f"/api/v1/site-templates/{tid}/use", json={})
    assert resp.status_code == 200, resp.text
    resp = await client.get("/api/v1/site-templates", params={"limit": 51})
    assert resp.status_code == 422, resp.text

    who.update(user=OWNER, ws=WS)
    resp = await client.post(f"/api/v1/site-templates/{tid}/report", json={"reason": "x"})
    assert resp.status_code == 403, resp.text
    resp = await client.patch(f"/api/v1/site-templates/{tid}", json={"visibility": "private"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["visibility"] == "private"

    who.update(user=STRANGER, ws=OTHER_WS)
    resp = await client.get(f"/api/v1/site-templates/{tid}")
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_route_publish_refusal_code(client: AsyncClient) -> None:
    src = await _site(
        source={"a.svelte": '<img src="/api/v1/media/1759300000000-abcdef012345.png">'}
    )
    resp = await client.post(
        "/api/v1/site-templates",
        json={"pocket_id": str(src.id), "name": "Tpl", "visibility": "public"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "site_templates.private_assets"
