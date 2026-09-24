# tests/ee/sites/test_component_edit.py — exercises the targeted svelte-component
# edit path (sites_service.edit_svelte_component). Created:
# 2026-06-17 (feat/sites-svelte-component-edit, SE-2).
#
# Updated 2026-09-24 (PP-2): the edit no longer builds a local preview; it VERIFIES the
# draft (static, sandbox build, browser). The headline test asserts the verifier saw the
# new source and nothing built on the host; the rollback tests drive a static / build
# FAILED verdict; browser failures and unverified verdicts stay staged.
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

    async def put_worker(self, *, script_name, bundle, bindings=None):
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
async def test_edit_component_verifies_the_new_source_and_stages_a_draft(
    beanie_test_db, edit_verifier
):
    """PP-2: a component edit is persisted and then VERIFIED — the verify pipeline sees
    the NEW Hero source — and it stays a reviewable draft. No local build runs, no CF
    worker is put, and no preview Site doc is minted (there is no preview url)."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    gen, cf = _FakeGenerator(), _FakeCF()

    result = await sites_service.edit_svelte_component(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/lib/components/Hero.svelte",
        new_source=_HERO_V2,
    )

    assert cf.put_calls == []
    assert gen.built is None, "the edit must not build on the API host any more"
    assert result.verification["status"] == "passed"
    # The verifier ran exactly once, over the EDITED source.
    assert len(edit_verifier.calls) == 1
    seen = edit_verifier.seen_sources[0]
    assert seen["src/lib/components/Hero.svelte"] == _HERO_V2
    assert seen["src/routes/+page.ts"] == "export const prerender = true"

    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == _HERO_V2

    from pocketpaw_ee.versions import service as versions

    draft = await versions.get_draft(scope_type="pocket", scope_id=pocket_id)
    assert draft is not None
    assert draft.content["src/lib/components/Hero.svelte"] == _HERO_V2


@pytest.mark.asyncio
@pytest.mark.parametrize("layer", ["static", "build"])
async def test_an_edit_that_does_not_compile_is_rolled_back(
    beanie_test_db, edit_verifier, layer
):
    """A STATIC or BUILD failure means the edit does not compile: the file is restored
    and ``EditVerificationFailed`` (a ``SmokeGateFailed``, so the old contract holds)
    carries the verdict for the agent."""
    from tests.ee.sites.conftest import verdict_with

    pocket_id = await _make_svelte_pocket("ws1", "u1")
    edit_verifier.verdict = verdict_with(**{layer: "failed"})

    broken = "<script>import x from 'not-declared'</script>"
    with pytest.raises(SmokeGateFailed) as info:
        await sites_service.edit_svelte_component(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/lib/components/Hero.svelte",
            new_source=broken,
        )

    assert isinstance(info.value, sites_service.EditVerificationFailed)
    assert info.value.verdict["status"] == "failed"
    # The broken edit WAS what got verified ...
    assert edit_verifier.seen_sources[0]["src/lib/components/Hero.svelte"] == broken
    # ... and the persisted source was rolled back to the last good contents.
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == _HERO_V1


@pytest.mark.asyncio
async def test_a_browser_failure_stays_staged_and_is_reported(beanie_test_db, edit_verifier):
    """The page compiles and the browser found a runtime defect: the edit STAYS staged
    (the fix is usually a follow-up edit to this very file) and the verdict says what
    broke. Rolling it back would throw the work away."""
    from tests.ee.sites.conftest import verdict_with

    pocket_id = await _make_svelte_pocket("ws1", "u1")
    edit_verifier.verdict = verdict_with(browser="failed")

    result = await sites_service.edit_svelte_component(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/lib/components/Hero.svelte",
        new_source=_HERO_V2,
    )

    assert result.verification["status"] == "failed"
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == _HERO_V2


@pytest.mark.asyncio
async def test_an_unverified_edit_stays_staged(beanie_test_db, edit_verifier):
    """Nothing proved the edit wrong (no sandbox, a timeout): it stays staged and the
    verdict is ``unverified`` — never reported as a pass."""
    from tests.ee.sites.conftest import verdict_with

    pocket_id = await _make_svelte_pocket("ws1", "u1")
    edit_verifier.verdict = verdict_with(build="unverified", browser="unverified")

    result = await sites_service.edit_svelte_component(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/lib/components/Hero.svelte",
        new_source=_HERO_V2,
    )
    assert result.verification["status"] == "unverified"
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == _HERO_V2


@pytest.mark.asyncio
async def test_a_verifier_that_raises_leaves_the_edit_staged_and_unverified(beanie_test_db):
    pocket_id = await _make_svelte_pocket("ws1", "u1")

    async def _boom(**_kw):
        raise RuntimeError("redis is down")

    result = await sites_service.edit_svelte_component(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/lib/components/Hero.svelte",
        new_source=_HERO_V2,
        _verify=_boom,
    )
    assert result.verification["status"] == "unverified"
    assert result.verification["reason"] == "verify_unavailable"
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == _HERO_V2


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
        )
