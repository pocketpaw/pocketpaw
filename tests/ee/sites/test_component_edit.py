# tests/ee/sites/test_component_edit.py — exercises the targeted svelte-component
# edit + republish path (sites_service.edit_svelte_component). Created:
# 2026-06-17 (feat/sites-svelte-component-edit, SE-2).
#
# Updated 2026-06-18 (fix/sites-edit-draft-not-publish): a component edit now
# builds a PREVIEW (local serve, draft kept) instead of a live CF deploy +
# promote-to-published. test_edit_component_republishes_with_new_source asserts the
# new contract: preview built (deployed=False, no CF put, preview URL returned), the
# pocket source persisted, and a reviewable draft left behind. The smoke-gate
# rollback test is unchanged (the gate fires before any deploy, preview or live).
#
# The flow under test: one component file of a svelte Paw Site pocket is
# rewritten and the site is safely republished. These tests use the shared
# ``beanie_test_db`` fixture (an in-memory Mongo) so the pockets service persists
# a real svelte Pocket doc, and inject a fake generator + CF client so no
# Bun/workerd/Cloudflare is touched. They prove:
#   (a) a component edit reaches the regenerated build (the new source is what
#       the generator materializes) and persists on the pocket;
#   (b) a deliberately-broken edit fails the smoke gate (SmokeGateFailed
#       propagates), the live deploy is unchanged, and the persisted source is
#       rolled back to the last good contents so the next publish is not broken;
#   (c) the standard not-found / wrong-engine / unknown-component guards.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError, NotFound
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.generator_client import SmokeGateFailed

# A minimal but §4.3-complete svelte source map — enough to stand in for a real
# Paw Site pocket. The component under edit is Hero.svelte.
_HERO_V1 = "<section class='hero'><h1>Bright Smile</h1></section>"
_HERO_V2 = "<section class='hero'><h1>Brighter Smiles, Whiter Teeth</h1></section>"
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
    """Records the source map it was asked to build so a test can assert the
    edited component reached the regenerated build."""

    def __init__(self):
        self.built = None

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.built = kw
        return BuildResult(project_dir="/tmp/site", ripple_version=None)


class _SmokeFailGenerator:
    """Stands in for a generator whose workerd smoke render fails — exactly how
    ``GeneratorClient.build`` signals a broken site (it raises SmokeGateFailed
    BEFORE any deploy)."""

    def __init__(self):
        self.built = None

    async def build(self, **kw):
        self.built = kw
        raise SmokeGateFailed("workerd SSR failure: document is not defined")


class _FakeCF:
    def __init__(self):
        self.put_calls = []

    async def put_worker(self, *, script_name, bundle):
        self.put_calls.append(script_name)
        return True


def _fake_local_deploy(site_id: str, project_dir: str) -> str:
    """Stand in for local_server.deploy_local so the PREVIEW serve (the edit path)
    does not need a real built dir on disk — returns the localhost preview URL."""
    return f"http://127.0.0.1:9999/{site_id}/"


@pytest.fixture(autouse=True)
def recording_bus():
    """Install a recording EventBus so the pockets service's ``emit`` calls
    (PocketCreated on create, PocketUpdated on the component edit) don't raise —
    the real bus is only wired by ``init_realtime()`` at boot. Mirrors the same
    fixture in tests/cloud/conftest.py / the sites-MCP tests."""
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
    """Persist a real svelte-engine Pocket via the pockets service and return its
    id. Mirrors how ``create_svelte_site`` lands a pocket: type='site',
    pattern='landing', engine='svelte', source=<map>, trusted=True."""
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
    assert pocket_id is not None
    return pocket_id


@pytest.mark.asyncio
async def test_edit_component_republishes_with_new_source(beanie_test_db):
    """A component edit is persisted on the pocket AND reaches the regenerated
    PREVIEW build: the generator materializes the NEW Hero source, not the old one.

    Branch primitive (fix/sites-edit-draft-not-publish): an edit now builds a
    PREVIEW (local serve) — it is NOT a live deploy, so ``deployed`` is False and no
    CF worker is put. The live deploy only happens on an approved review."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    gen, cf = _FakeGenerator(), _FakeCF()

    site = await sites_service.edit_svelte_component(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/lib/components/Hero.svelte",
        new_source=_HERO_V2,
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )

    # An edit is a PREVIEW, not a live deploy: no CF worker put, not deployed,
    # but it returns the STABLE per-pocket preview URL the builder iframe frames
    # (preview-<pocket_id>, NOT the minted site id — so repeated builds don't churn
    # the url).
    assert cf.put_calls == []
    assert site.deployed is False
    assert site.url.endswith(f"/preview-{pocket_id}/")
    assert site.pocket_id == pocket_id
    # The regenerated build materialized the EDITED component, not the original.
    assert gen.built is not None
    assert gen.built["engine"] == "svelte"
    assert gen.built["source"]["src/lib/components/Hero.svelte"] == _HERO_V2
    # Untouched files came through verbatim.
    assert gen.built["source"]["src/routes/+page.ts"] == "export const prerender = true"

    # The edit persisted on the pocket — a re-read shows the new source.
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == _HERO_V2

    # And it left a reviewable DRAFT — not promoted to published (the bug).
    from pocketpaw_ee.versions import service as versions

    draft = await versions.get_draft(scope_type="pocket", scope_id=pocket_id)
    assert draft is not None
    assert draft.content["src/lib/components/Hero.svelte"] == _HERO_V2


@pytest.mark.asyncio
async def test_broken_edit_propagates_smoke_gate_and_rolls_back(beanie_test_db):
    """A deliberately-broken edit fails the smoke gate: SmokeGateFailed
    propagates, NO worker is deployed (the prior deploy stays), and the persisted
    source is ROLLED BACK to the last good contents so the next publish is not
    broken."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    gen, cf = _SmokeFailGenerator(), _FakeCF()

    broken = "<script>onMount(() => { throw new Error('boom') })</script>"
    with pytest.raises(SmokeGateFailed):
        await sites_service.edit_svelte_component(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/lib/components/Hero.svelte",
            new_source=broken,
            _generator=gen,
            _cloudflare=cf,
            _bundle_reader=lambda d: b"export default {}",
        )

    # The smoke gate fired BEFORE any deploy — no worker was put.
    assert cf.put_calls == []
    # The broken edit WAS handed to the generator (so the gate saw it) ...
    assert gen.built["source"]["src/lib/components/Hero.svelte"] == broken
    # ... but the persisted source was rolled back to the last good contents, so a
    # later publish would not rebuild the broken page.
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == _HERO_V1


@pytest.mark.asyncio
async def test_edit_unknown_component_raises_not_found(beanie_test_db):
    """Editing a component path that does not exist in the pocket's source map
    raises NotFound (404) — the agent gets a clear 'no such component', not a
    silent create or a 500."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    with pytest.raises(NotFound):
        await sites_service.edit_svelte_component(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/lib/components/DoesNotExist.svelte",
            new_source="<section/>",
            _generator=_FakeGenerator(),
            _cloudflare=_FakeCF(),
            _bundle_reader=lambda d: b"x",
        )


@pytest.mark.asyncio
async def test_edit_missing_pocket_raises_not_found(beanie_test_db):
    """A missing pocket id raises NotFound, mapped by callers to a 404 / is_error."""
    with pytest.raises(NotFound):
        await sites_service.edit_svelte_component(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="0123456789abcdef01234567",
            component_path="src/lib/components/Hero.svelte",
            new_source="<section/>",
            _generator=_FakeGenerator(),
            _cloudflare=_FakeCF(),
            _bundle_reader=lambda d: b"x",
        )


@pytest.mark.asyncio
async def test_edit_ripple_pocket_rejected(beanie_test_db):
    """A ripple-engine pocket has no svelte ``source`` map — editing a component
    on it is a clear CloudError, not a None-deref or a 500."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Ripple Pocket",
        type_="site",
        pattern="landing",
        ripple_spec={"type": "container"},
        # engine defaults to "ripple", source stays None
    )
    assert err is None, err
    with pytest.raises(CloudError):
        await sites_service.edit_svelte_component(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/lib/components/Hero.svelte",
            new_source="<section/>",
            _generator=_FakeGenerator(),
            _cloudflare=_FakeCF(),
            _bundle_reader=lambda d: b"x",
        )
