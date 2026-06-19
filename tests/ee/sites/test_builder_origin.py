# tests/ee/sites/test_builder_origin.py — exercises threading builderOrigin
# through the publish path so a svelte Paw Site stays editable across republish.
# Created: 2026-06-17 (feat/sites-svelte-component-edit, SE-2b).
#
# Background: SE-1 (paw-sites generator) gates the editable section anchors + the
# postMessage edit-bridge on ``SiteConfig.builderOrigin``. The publish path never
# set it, so (a) a site was never editable and (b) an edit-republish would strip
# the bridge. These tests pin the pocketpaw side: publish/publish_pocket thread a
# ``builder_origin`` to the generator AND store it on the Site doc;
# edit_svelte_component reuses the STORED value so an editable site stays editable
# after a component edit; and make_site_editable republishes a site as editable.
#
# Fakes inject the generator + CF client so no Bun/workerd/Cloudflare is touched.
#
# Updated 2026-06-18 (fix/sites-edit-draft-not-publish): edit_svelte_component /
# make_site_editable now take the PREVIEW branch (local serve, no live promote), so
# the edit/arm-path tests inject a fake ``_local_deploy`` instead of relying on the
# CF deploy. The builder_origin threading these tests pin is unchanged — a preview
# still forwards the origin to the generator and carries it on the returned Site.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import service as sites_service

_HERO_V1 = "<section class='hero'><h1>Bright Smile</h1></section>"
_HERO_V2 = "<section class='hero'><h1>Brighter Smiles</h1></section>"
_SVELTE_SOURCE = {
    "src/routes/+page.svelte": (
        "<script>import Hero from '$lib/components/Hero.svelte'</script><Hero/>"
    ),
    "src/routes/+layout.svelte": "<script>import '../app.css'</script><slot/>",
    "src/routes/+page.ts": "export const prerender = true",
    "src/app.css": ":root{--brand:#0A84FF}",
    "src/lib/components/Hero.svelte": _HERO_V1,
}


class _FakeGenerator:
    def __init__(self):
        self.built = None

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.built = kw
        return BuildResult(project_dir="/tmp/site", ripple_version=None)


class _FakeCF:
    def __init__(self):
        self.put_calls = []

    async def put_worker(self, *, script_name, bundle):
        self.put_calls.append(script_name)
        return True


def _fake_local_deploy(site_id: str, project_dir: str) -> str:
    """Stand in for local_server.deploy_local so the PREVIEW serve (the edit/arm
    path) does not need a real built dir on disk — returns the localhost URL."""
    return f"http://127.0.0.1:9999/{site_id}/"


@pytest.fixture(autouse=True)
def recording_bus():
    """Install a recording EventBus so pockets-service emits don't raise."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.events import Event

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Event] = []

        async def publish(self, event: Event) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler) -> None:  # noqa: ARG002
            return

    rec = _RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]


async def _make_svelte_pocket(workspace_id: str, user_id: str) -> str:
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=workspace_id,
        owner_id=user_id,
        name="Bright Smile",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine="svelte",
        source=dict(_SVELTE_SOURCE),
        trusted=True,
    )
    assert err is None, err
    return pocket_id


@pytest.mark.asyncio
async def test_publish_stores_builder_origin_and_forwards_to_generator(beanie_test_db):
    """publish(builder_origin=...) hands it to the generator AND persists it on
    the Site doc, so a later republish can recover it."""
    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="x",
        builder_origin="https://app.paw.example",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    assert gen.built["builder_origin"] == "https://app.paw.example"
    assert site.builder_origin == "https://app.paw.example"
    fresh = await _SiteDoc.get(site.id)
    assert fresh.builder_origin == "https://app.paw.example"


@pytest.mark.asyncio
async def test_publish_default_builder_origin_is_empty(beanie_test_db):
    """A publish with no builder_origin stores "" and does NOT forward an origin
    to the generator — the site is not editable (no bridge)."""
    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="x",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    assert site.builder_origin == ""
    # build() got no origin (None), so the generator omits builderOrigin.
    assert gen.built.get("builder_origin") in (None, "")


@pytest.mark.asyncio
async def test_publish_pocket_threads_builder_origin(beanie_test_db):
    """publish_pocket(builder_origin=...) forwards it through to publish + the
    generator."""
    from unittest.mock import AsyncMock, patch

    gen, cf = _FakeGenerator(), _FakeCF()
    wire = {"name": "Bright Smile", "rippleSpec": {"type": "container"}}
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ):
        site = await sites_service.publish_pocket(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            builder_origin="https://app.paw.example",
            _generator=gen,
            _cloudflare=cf,
            _bundle_reader=lambda d: b"x",
        )
    assert gen.built["builder_origin"] == "https://app.paw.example"
    assert site.builder_origin == "https://app.paw.example"


@pytest.mark.asyncio
async def test_edit_component_preserves_stored_builder_origin(beanie_test_db):
    """The key SE-2b guarantee: editing a component of an EDITABLE site keeps it
    editable. The component-edit republish recovers the builder_origin stored on
    the prior Site doc and re-sends it to the generator, so the bridge survives."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")

    # First publish the site as EDITABLE (builder_origin set).
    first = await sites_service.publish_pocket(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin="https://app.paw.example",
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
    )
    assert first.builder_origin == "https://app.paw.example"

    # Now edit a component. The republish must re-apply the stored origin.
    gen = _FakeGenerator()
    site = await sites_service.edit_svelte_component(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/lib/components/Hero.svelte",
        new_source=_HERO_V2,
        _generator=gen,
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
        _local_deploy=_fake_local_deploy,
    )
    # The edited site is STILL editable — builder_origin carried through.
    assert gen.built["builder_origin"] == "https://app.paw.example"
    assert site.builder_origin == "https://app.paw.example"


@pytest.mark.asyncio
async def test_edit_component_on_non_editable_site_stays_non_editable(beanie_test_db):
    """A site published WITHOUT a builder_origin stays non-editable after an
    edit — the republish does not invent an origin."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    await sites_service.publish_pocket(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
    )
    gen = _FakeGenerator()
    site = await sites_service.edit_svelte_component(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/lib/components/Hero.svelte",
        new_source=_HERO_V2,
        _generator=gen,
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
        _local_deploy=_fake_local_deploy,
    )
    assert site.builder_origin == ""
    assert gen.built.get("builder_origin") in (None, "")


@pytest.mark.asyncio
async def test_make_site_editable_republishes_with_origin(beanie_test_db):
    """make_site_editable republishes the pocket's site WITH a builder_origin so
    the generator injects the edit-bridge. Backs POST
    /sites/by-pocket/{pocket_id}/editable."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    # Start non-editable.
    await sites_service.publish_pocket(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
    )
    gen = _FakeGenerator()
    site = await sites_service.make_site_editable(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin="https://app.paw.example",
        _generator=gen,
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
        _local_deploy=_fake_local_deploy,
    )
    assert site.builder_origin == "https://app.paw.example"
    assert gen.built["builder_origin"] == "https://app.paw.example"


@pytest.mark.asyncio
async def test_make_site_editable_defaults_builder_origin_from_config(beanie_test_db, monkeypatch):
    """When no builder_origin is passed, make_site_editable falls back to the
    configured PAW_SITES_BUILDER_ORIGIN so the endpoint works with no arg."""
    monkeypatch.setenv("PAW_SITES_BUILDER_ORIGIN", "https://configured.paw.example")
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    gen = _FakeGenerator()
    site = await sites_service.make_site_editable(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        _generator=gen,
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"x",
        _local_deploy=_fake_local_deploy,
    )
    assert site.builder_origin == "https://configured.paw.example"
    assert gen.built["builder_origin"] == "https://configured.paw.example"
