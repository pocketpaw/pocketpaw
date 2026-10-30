# tests/ee/sites/test_engine_field.py — SR-9: a published site surfaces its
# SOURCE pocket's authoring ``engine`` ("svelte" | "ripple") on the sites-list
# (list_for_workspace) and the by-pocket status (pocket_status) responses, so the
# gallery can badge each card's engine (Custom vs Ripple) without a second fetch.
#
# The engine lives on Pocket.engine, not the Site, so the service resolves it via
# pockets_service.engines_for_pockets — the sibling of DS-1a's patterns_for_pockets
# (see test_pattern_field.py, which this mirrors). These tests seed a REAL Pocket
# doc (so its ObjectId can be matched), publish a Site whose pocket_id == that doc's
# id, and assert the response carries the pocket's engine. Empty-safe cases
# (missing pocket) read "". The same two reads carry ``capabilities`` for every
# engine, with ``has_database`` from the dynamic pattern/spec, the d1-drizzle
# recipe or the Site's bound D1; an unresolved pocket reads None.
#
# Created: 2026-07-09 (feat/sites-engine-field, SR-9).
from __future__ import annotations

import pytest
from bson import ObjectId
from pocketpaw_ee.sites import service as sites_service


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakeCF:
    def __init__(self):
        self.put_calls = []

    async def put_worker(self, *, script_name, bundle, bindings=None):
        self.put_calls.append(script_name)
        return True


async def _seed_pocket(*, workspace_id: str, owner: str, name: str, engine: str) -> str:
    """Insert a real Pocket doc with the given engine and return its id (the
    wire-string ObjectId). Returns the id so the published Site's ``pocket_id`` can
    point at it — the engine resolution matches on the Pocket's ``_id``."""
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(
        workspace=workspace_id,
        name=name,
        owner=owner,
        type="site",
        engine=engine,
    )
    await doc.insert()
    return str(doc.id)


async def _publish_for_pocket(*, workspace_id: str, pocket_id: str) -> None:
    """Publish a site for an existing pocket. ``name`` is passed explicitly so
    publish does not try to read the pocket through the full access path."""
    await sites_service.publish(
        workspace_id=workspace_id,
        user_id="u1",
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={},
        name="x",
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
    )


@pytest.mark.asyncio
async def test_list_for_workspace_surfaces_svelte_engine(beanie_test_db):
    """A site published from a pocket with engine="svelte" lists with
    engine == "svelte"."""
    pocket_id = await _seed_pocket(
        workspace_id="ws1", owner="u1", name="Custom Site", engine="svelte"
    )
    await _publish_for_pocket(workspace_id="ws1", pocket_id=pocket_id)

    sites = await sites_service.list_for_workspace("ws1")

    assert len(sites) == 1
    assert sites[0].pocket_id == pocket_id
    assert sites[0].engine == "svelte"


@pytest.mark.asyncio
async def test_list_for_workspace_distinguishes_engines(beanie_test_db):
    """A svelte and a ripple site in the same workspace each surface their own
    source pocket's engine — the field is per-card, resolved from the pocket."""
    svelte = await _seed_pocket(workspace_id="ws1", owner="u1", name="Custom", engine="svelte")
    ripple = await _seed_pocket(workspace_id="ws1", owner="u1", name="Marketing", engine="ripple")
    await _publish_for_pocket(workspace_id="ws1", pocket_id=svelte)
    await _publish_for_pocket(workspace_id="ws1", pocket_id=ripple)

    by_pocket = {s.pocket_id: s.engine for s in await sites_service.list_for_workspace("ws1")}

    assert by_pocket[svelte] == "svelte"
    assert by_pocket[ripple] == "ripple"


@pytest.mark.asyncio
async def test_list_for_workspace_empty_safe_when_pocket_missing(beanie_test_db):
    """A site whose source pocket no longer exists (deleted, or a non-ObjectId
    pocket_id) reads "" for engine rather than crashing the list."""
    await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="not-an-objectid",  # cannot resolve to a Pocket doc
        ripple_spec={"type": "container"},
        theme={},
        name="x",
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
    )

    sites = await sites_service.list_for_workspace("ws1")

    assert len(sites) == 1
    assert sites[0].engine == ""


@pytest.mark.asyncio
async def test_pocket_status_surfaces_engine(beanie_test_db):
    """The by-pocket status read carries the source pocket's engine too, so a
    builder/gallery status fetch can badge the engine."""
    pocket_id = await _seed_pocket(workspace_id="ws1", owner="u1", name="Custom", engine="svelte")
    await _publish_for_pocket(workspace_id="ws1", pocket_id=pocket_id)

    res = await sites_service.pocket_status(workspace_id="ws1", pocket_id=pocket_id)

    assert res.engine == "svelte"


@pytest.mark.asyncio
async def test_pocket_status_engine_is_tenant_scoped(beanie_test_db):
    """A pocket's engine does not leak across workspaces: a foreign workspace's
    status read for the same pocket id reads "" (the batch read is workspace-
    scoped)."""
    pocket_id = await _seed_pocket(
        workspace_id="ws_owner", owner="u1", name="Private", engine="svelte"
    )
    await _publish_for_pocket(workspace_id="ws_owner", pocket_id=pocket_id)

    res = await sites_service.pocket_status(workspace_id="ws_intruder", pocket_id=pocket_id)

    assert res.engine == ""


@pytest.mark.asyncio
async def test_engines_for_pockets_batch_read(beanie_test_db):
    """The pockets-service helper returns a {pocket_id: engine} map in one read,
    tenant-scoped, with missing/cross-tenant ids simply absent."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    a = await _seed_pocket(workspace_id="ws1", owner="u1", name="A", engine="svelte")
    b = await _seed_pocket(workspace_id="ws1", owner="u1", name="B", engine="ripple")
    other = await _seed_pocket(workspace_id="ws2", owner="u1", name="C", engine="svelte")
    missing = str(ObjectId())

    out = await pockets_service.engines_for_pockets("ws1", [a, b, other, missing, "garbage"])

    assert out == {a: "svelte", b: "ripple"}


# ---------------------------------------------------- capabilities on the site reads


async def _draft_site(workspace_id: str = "ws1", **pocket_fields) -> str:
    """Seed a site pocket with ``pocket_fields`` and mint its DRAFT Site doc (no
    publish, so any engine works). Returns the pocket id."""
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    pocket_fields.setdefault("engine", "svelte")
    doc = _PocketDoc(workspace=workspace_id, name="S", owner="u1", type="site", **pocket_fields)
    await doc.insert()
    await sites_service.create_draft_site(
        workspace_id=workspace_id, user_id="u1", pocket_id=str(doc.id), name="S"
    )
    return str(doc.id)


async def _listed_caps(workspace_id: str = "ws1") -> dict:
    return {
        s.pocket_id: s.capabilities for s in await sites_service.list_for_workspace(workspace_id)
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["ripple", "svelte", "html", "react", "project"])
async def test_list_and_status_carry_capabilities_for_every_engine(beanie_test_db, engine):
    from pocketpaw_ee.sites.engines import site_capabilities

    pocket_id = await _draft_site(engine=engine)
    expected = site_capabilities(engine, has_database=False)

    assert (await _listed_caps())[pocket_id] == expected
    status = await sites_service.pocket_status(workspace_id="ws1", pocket_id=pocket_id)
    assert status.capabilities == expected


@pytest.mark.asyncio
async def test_a_legacy_pocket_without_an_engine_reads_ripple_capabilities(beanie_test_db):
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
    from pocketpaw_ee.sites.engines import site_capabilities

    pocket_id = await _draft_site(engine="ripple")
    await _PocketDoc.get_pymongo_collection().update_one(
        {"_id": ObjectId(pocket_id)}, {"$unset": {"engine": ""}}
    )

    assert (await _listed_caps())[pocket_id] == site_capabilities("ripple", has_database=False)


@pytest.mark.asyncio
async def test_has_database_from_the_dynamic_pattern(beanie_test_db):
    pocket_id = await _draft_site(engine="ripple", pattern="dynamic")
    assert (await _listed_caps())[pocket_id]["has_database"] is True


@pytest.mark.asyncio
async def test_has_database_from_a_spec_that_declares_actions(beanie_test_db):
    pocket_id = await _draft_site(
        engine="ripple", pattern="landing", rippleSpec={"ui": {}, "actions": {"add": {}}}
    )
    assert (await _listed_caps())[pocket_id]["has_database"] is True


@pytest.mark.asyncio
async def test_has_database_from_the_d1_recipe(beanie_test_db):
    with_db = await _draft_site(
        engine="project",
        site_meta={
            "project": {"template": "astro", "framework": "astro", "recipes": ["d1-drizzle"]}
        },
    )
    without = await _draft_site(
        engine="project",
        site_meta={"project": {"template": "astro", "framework": "astro", "recipes": ["kv"]}},
    )
    caps = await _listed_caps()
    assert caps[with_db]["has_database"] is True
    assert caps[without]["has_database"] is False
    status = await sites_service.pocket_status(workspace_id="ws1", pocket_id=with_db)
    assert status.capabilities["has_database"] is True


@pytest.mark.asyncio
async def test_has_database_from_the_sites_bound_d1(beanie_test_db):
    from pocketpaw_ee.cloud.models.site import Site

    pocket_id = await _draft_site(engine="svelte", pattern="landing")
    site = await Site.find_one(Site.pocket_id == pocket_id)
    site.d1_database_id = "11111111-2222-3333-4444-555555555555"
    await site.save()

    assert (await _listed_caps())[pocket_id]["has_database"] is True
    status = await sites_service.pocket_status(workspace_id="ws1", pocket_id=pocket_id)
    assert status.capabilities["has_database"] is True


@pytest.mark.asyncio
async def test_an_unresolved_pocket_has_no_capabilities(beanie_test_db):
    pocket_id = await _draft_site(workspace_id="ws_owner")

    status = await sites_service.pocket_status(workspace_id="ws_intruder", pocket_id=pocket_id)
    assert status.engine == ""
    assert status.capabilities is None


@pytest.mark.asyncio
async def test_site_database_inputs_for_pockets_batch_read(beanie_test_db):
    """One tenant-scoped projected read; never the whole rippleSpec."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    a = await _draft_site(
        engine="ripple",
        pattern="dynamic",
        rippleSpec={"ui": {"big": "x" * 100}, "sources": {"rows": {}}},
    )
    b = await _draft_site(
        engine="project", site_meta={"project": {"template": "t", "recipes": ["d1-drizzle"]}}
    )
    other = await _draft_site(workspace_id="ws2")

    out = await pockets_service.site_database_inputs_for_pockets(
        "ws1", [a, b, other, str(ObjectId()), "garbage"]
    )

    assert set(out) == {a, b}
    assert out[a]["pattern"] == "dynamic"
    assert out[a]["ripple_spec"] == {"sources": {"rows": {}}}
    assert out[b]["site_meta"] == {"project": {"recipes": ["d1-drizzle"]}}
