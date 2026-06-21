# tests/ee/sites/test_pattern_field.py — DS-1a: a published site surfaces its
# SOURCE pocket's authoring ``pattern`` ("dynamic" | "landing" | ...) on the
# sites-list (list_for_workspace) and the by-pocket status (pocket_status)
# responses, so the frontend can badge dynamic sites.
#
# The pattern lives on Pocket.pattern, not the Site, so the service resolves it via
# pockets_service.patterns_for_pockets. These tests seed a REAL Pocket doc (so its
# ObjectId can be matched), publish a Site whose pocket_id == that doc's id, and
# assert the response carries the pocket's pattern. Empty-safe cases (no pattern,
# missing pocket) read "".
#
# Created: 2026-06-20 (feat/sites-pattern-field, DS-1a).
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

    async def put_worker(self, *, script_name, bundle):
        self.put_calls.append(script_name)
        return True


async def _seed_pocket(*, workspace_id: str, owner: str, name: str, pattern: str | None) -> str:
    """Insert a real Pocket doc and return its id (the wire-string ObjectId).

    Returns the id so the published Site's ``pocket_id`` can point at it — the
    pattern resolution matches on the Pocket's ``_id``."""
    from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

    doc = _PocketDoc(
        workspace=workspace_id,
        name=name,
        owner=owner,
        type="site",
        pattern=pattern,
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
async def test_list_for_workspace_surfaces_dynamic_pattern(beanie_test_db):
    """A site published from a pocket with pattern="dynamic" lists with
    pattern == "dynamic"."""
    pocket_id = await _seed_pocket(
        workspace_id="ws1", owner="u1", name="Guestbook", pattern="dynamic"
    )
    await _publish_for_pocket(workspace_id="ws1", pocket_id=pocket_id)

    sites = await sites_service.list_for_workspace("ws1")

    assert len(sites) == 1
    assert sites[0].pocket_id == pocket_id
    assert sites[0].pattern == "dynamic"


@pytest.mark.asyncio
async def test_list_for_workspace_distinguishes_patterns(beanie_test_db):
    """A dynamic and a landing site in the same workspace each surface their own
    source pocket's pattern — the field is per-card, resolved from the pocket."""
    dyn = await _seed_pocket(workspace_id="ws1", owner="u1", name="Bookings", pattern="dynamic")
    landing = await _seed_pocket(workspace_id="ws1", owner="u1", name="Dental", pattern="landing")
    await _publish_for_pocket(workspace_id="ws1", pocket_id=dyn)
    await _publish_for_pocket(workspace_id="ws1", pocket_id=landing)

    by_pocket = {s.pocket_id: s.pattern for s in await sites_service.list_for_workspace("ws1")}

    assert by_pocket[dyn] == "dynamic"
    assert by_pocket[landing] == "landing"


@pytest.mark.asyncio
async def test_list_for_workspace_empty_safe_when_pocket_has_no_pattern(beanie_test_db):
    """A site whose source pocket has no pattern (None) reads "" — no crash, the
    gallery stays empty-safe."""
    pocket_id = await _seed_pocket(workspace_id="ws1", owner="u1", name="Legacy", pattern=None)
    await _publish_for_pocket(workspace_id="ws1", pocket_id=pocket_id)

    sites = await sites_service.list_for_workspace("ws1")

    assert len(sites) == 1
    assert sites[0].pattern == ""


@pytest.mark.asyncio
async def test_list_for_workspace_empty_safe_when_pocket_missing(beanie_test_db):
    """A site whose source pocket no longer exists (deleted, or a non-ObjectId
    pocket_id) reads "" rather than crashing the list."""
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
    assert sites[0].pattern == ""


@pytest.mark.asyncio
async def test_pocket_status_surfaces_dynamic_pattern(beanie_test_db):
    """The by-pocket status read carries the source pocket's pattern too, so a
    builder/gallery status fetch can badge a dynamic site."""
    pocket_id = await _seed_pocket(workspace_id="ws1", owner="u1", name="Orders", pattern="dynamic")
    await _publish_for_pocket(workspace_id="ws1", pocket_id=pocket_id)

    res = await sites_service.pocket_status(workspace_id="ws1", pocket_id=pocket_id)

    assert res.pattern == "dynamic"


@pytest.mark.asyncio
async def test_pocket_status_pattern_is_tenant_scoped(beanie_test_db):
    """A pocket's pattern does not leak across workspaces: a foreign workspace's
    status read for the same pocket id reads "" (the batch read is workspace-
    scoped)."""
    pocket_id = await _seed_pocket(
        workspace_id="ws_owner", owner="u1", name="Private", pattern="dynamic"
    )
    await _publish_for_pocket(workspace_id="ws_owner", pocket_id=pocket_id)

    res = await sites_service.pocket_status(workspace_id="ws_intruder", pocket_id=pocket_id)

    assert res.pattern == ""


@pytest.mark.asyncio
async def test_patterns_for_pockets_batch_read(beanie_test_db):
    """The pockets-service helper returns a {pocket_id: pattern} map in one read,
    tenant-scoped, with missing/cross-tenant ids simply absent."""
    from pocketpaw_ee.cloud.pockets import service as pockets_service

    a = await _seed_pocket(workspace_id="ws1", owner="u1", name="A", pattern="dynamic")
    b = await _seed_pocket(workspace_id="ws1", owner="u1", name="B", pattern="landing")
    other = await _seed_pocket(workspace_id="ws2", owner="u1", name="C", pattern="dynamic")
    missing = str(ObjectId())

    out = await pockets_service.patterns_for_pockets("ws1", [a, b, other, missing, "garbage"])

    assert out == {a: "dynamic", b: "landing"}
