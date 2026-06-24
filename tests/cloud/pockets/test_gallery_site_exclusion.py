# tests/cloud/pockets/test_gallery_site_exclusion.py
# Created: 2026-06-03 (Sites fix A) — pins that the desktop /pockets gallery
# hides pockets already published as Paw Sites, while keeping every other
# list_pockets caller (mission control, kb, surface, planners) unchanged.
#
# Covers:
#   * sites_service.site_pocket_ids — returns the set of pocket_ids that have a
#     Site in the workspace (tenant-scoped).
#   * the GALLERY route handler (pockets.router.list_pockets) — returns only the
#     non-site pocket once one of two pockets is published as a Site.
#   * list_pockets WITHOUT the exclude kwarg — still returns BOTH pockets, so the
#     exclusion is opt-in and the existing callers don't regress.
#
# Uses the shared ``mongo_db`` fixture (tests/cloud/conftest.py) which inits
# Beanie against an in-memory Mongo with ALL_DOCUMENTS (Pocket + Site both
# registered) and auto-installs a RecordingBus so the create() emit() succeeds.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest

# The gallery route HANDLER (not the APIRouter — pockets/__init__ re-exports the
# APIRouter as ``router``, so import the handler from the submodule by path).
from pocketpaw_ee.cloud.pockets.router import list_pockets as gallery_list_route
from pocketpaw_ee.sites import service as sites_service

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws_gallery"
_USER = "user_gallery"


class _FakeGenerator:
    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FakeCF:
    async def put_worker(self, *, script_name, bundle, bindings=None):
        return True


async def _make_pocket(name: str) -> str:
    """Create a pocket via the service and return its id."""
    wire = await pockets_service.create(_WS, _USER, CreatePocketRequest(name=name))
    return wire["_id"]


async def _publish_as_site(pocket_id: str) -> None:
    """Publish a pocket as a Site through the real service boundary (fakes for
    the generator + Cloudflare), so a Site doc with the right pocket_id lands."""
    await sites_service.publish(
        workspace_id=_WS,
        user_id=_USER,
        pocket_id=pocket_id,
        ripple_spec={"type": "container"},
        theme={},
        name="Published Site",
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"export default {}",
    )


@pytest.mark.asyncio
async def test_site_pocket_ids_returns_published_pocket_ids() -> None:
    plain_id = await _make_pocket("Plain Pocket")
    site_pocket_id = await _make_pocket("Site Pocket")
    await _publish_as_site(site_pocket_id)

    ids = await sites_service.site_pocket_ids(_WS)

    assert ids == {site_pocket_id}
    assert plain_id not in ids


@pytest.mark.asyncio
async def test_site_pocket_ids_empty_when_no_sites() -> None:
    await _make_pocket("Lonely Pocket")
    assert await sites_service.site_pocket_ids(_WS) == set()


@pytest.mark.asyncio
async def test_site_pocket_ids_is_tenant_scoped() -> None:
    site_pocket_id = await _make_pocket("Site Pocket")
    await _publish_as_site(site_pocket_id)

    # A different workspace sees none of this workspace's site pockets.
    assert await sites_service.site_pocket_ids("ws_other") == set()


@pytest.mark.asyncio
async def test_gallery_route_excludes_published_site_pocket() -> None:
    plain_id = await _make_pocket("Plain Pocket")
    site_pocket_id = await _make_pocket("Site Pocket")
    await _publish_as_site(site_pocket_id)

    # Call the gallery route handler directly with explicit args (overriding the
    # FastAPI Depends defaults) so we exercise the handler's exclusion wiring.
    result = await gallery_list_route(workspace_id=_WS, user_id=_USER, project_id=None)

    returned_ids = {p["_id"] for p in result}
    assert plain_id in returned_ids
    assert site_pocket_id not in returned_ids
    assert returned_ids == {plain_id}


@pytest.mark.asyncio
async def test_plain_list_pockets_still_returns_both_no_regression() -> None:
    plain_id = await _make_pocket("Plain Pocket")
    site_pocket_id = await _make_pocket("Site Pocket")
    await _publish_as_site(site_pocket_id)

    # The service-level list_pockets (no exclude kwarg) is what mission control,
    # kb, surface, and planners call — it must keep returning EVERY pocket.
    result = await pockets_service.list_pockets(_WS, _USER)

    returned_ids = {p["_id"] for p in result}
    assert returned_ids == {plain_id, site_pocket_id}


@pytest.mark.asyncio
async def test_list_pockets_exclude_kwarg_drops_only_named_ids() -> None:
    keep_id = await _make_pocket("Keep")
    drop_id = await _make_pocket("Drop")

    result = await pockets_service.list_pockets(_WS, _USER, exclude_pocket_ids={drop_id})

    returned_ids = {p["_id"] for p in result}
    assert returned_ids == {keep_id}
