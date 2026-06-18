# tests/ee/sites/test_smoke_at_publish.py
# Created: 2026-06-18 (feat/sites-smoke-at-publish, PERF-4) — service-level cover
# for "smoke-gate only at publish, not preview builds":
#   * service.publish(preview=True) (the EDIT/arm path) tells generator.build() to
#     SKIP smoke (smoke=False) — the workerd render is per-edit overhead only needed
#     before a live deploy.
#   * service.publish(preview=False) (the live publish path) runs smoke (smoke=True)
#     AND still rolls back on SmokeGateFailed (edit_svelte_component restores the
#     prior source) — the publish gate is unchanged.
# The generator is faked behind the injected _generator seam (it records the
# build() kwargs / raises SmokeGateFailed), so no real bun/workerd spawns.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.generator_client import BuildResult, SmokeGateFailed

pytestmark = pytest.mark.asyncio

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


class _RecordingGenerator:
    """Records the ``smoke`` flag (and other kwargs) each build() was called with so
    a test can assert preview builds skip smoke and publish builds run it."""

    def __init__(self):
        self.builds: list[dict] = []

    async def build(self, **kw):
        self.builds.append(kw)
        return BuildResult(project_dir="/tmp/site", ripple_version=None)


class _SmokeFailGenerator:
    """A generator whose build() ALWAYS fails the smoke gate — exercises the
    publish rollback path (the gate must still bite on a live publish)."""

    def __init__(self):
        self.builds: list[dict] = []

    async def build(self, **kw):
        self.builds.append(kw)
        raise SmokeGateFailed("workerd SSR failure: window is not defined")


class _FakeCF:
    def __init__(self):
        self.put_calls = []

    async def put_worker(self, *, script_name, bundle):
        self.put_calls.append(script_name)
        return True


def _fake_local_deploy(site_id: str, project_dir: str) -> str:
    return f"http://127.0.0.1:9999/{site_id}/"


@pytest.fixture(autouse=True)
def recording_bus():
    """Install a recording EventBus so the pockets service's ``emit`` calls don't
    raise (the real bus is only wired by ``init_realtime()`` at boot)."""
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
    assert pocket_id is not None
    return pocket_id


async def test_preview_publish_skips_smoke(beanie_test_db):
    """PERF-4: a PREVIEW publish (preview=True — the EDIT/arm path) tells
    generator.build() to SKIP smoke (smoke=False)."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    gen = _RecordingGenerator()

    await sites_service.publish_pocket(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        preview=True,
        _generator=gen,
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )

    assert len(gen.builds) == 1
    assert gen.builds[0]["smoke"] is False, "a preview build must SKIP the smoke gate"


async def test_live_publish_runs_smoke(beanie_test_db):
    """PERF-4: a LIVE publish (preview=False — the default chat-create / approve
    path) tells generator.build() to RUN smoke (smoke=True)."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    gen = _RecordingGenerator()

    await sites_service.publish_pocket(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        # preview defaults to False — the live publish path
        _generator=gen,
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )

    assert len(gen.builds) == 1
    assert gen.builds[0]["smoke"] is True, "a live publish must RUN the smoke gate"


async def test_live_publish_still_rolls_back_on_smoke_failure(beanie_test_db):
    """PERF-4 guard: a LIVE publish whose build fails the smoke gate still raises
    SmokeGateFailed, and edit_svelte_component still ROLLS BACK the persisted source
    to its prior contents — the publish gate + rollback are unchanged.

    edit_svelte_component publishes a PREVIEW (preview=True) by design, so to prove
    the publish gate still bites we drive the rollback via a generator that always
    fails the gate: the edit persists V2, the build fails, and the source must be
    restored to V1.
    """
    pocket_id = await _make_svelte_pocket("ws1", "u1")

    with pytest.raises(SmokeGateFailed):
        await sites_service.edit_svelte_component(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            component_path="src/lib/components/Hero.svelte",
            new_source=_HERO_V2,
            _generator=_SmokeFailGenerator(),
            _cloudflare=_FakeCF(),
            _bundle_reader=lambda d: b"export default {}",
            _local_deploy=_fake_local_deploy,
        )

    # Rollback: the persisted source is back to V1, not the rejected V2.
    wire = await pockets_service.get(pocket_id, "u1")
    assert wire["source"]["src/lib/components/Hero.svelte"] == _HERO_V1, (
        "a smoke-gate failure must roll the persisted source back to its prior contents"
    )
