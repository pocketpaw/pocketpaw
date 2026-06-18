# tests/ee/sites/test_edit_draft_not_publish.py — reproduces + locks the
# Branch-primitive bug where editing a site AUTO-PUBLISHED it instead of leaving a
# reviewable draft. Created: 2026-06-18 (fix/sites-edit-draft-not-publish).
#
# The bug: make_site_editable() and edit_svelte_component() both routed through
# publish_pocket → publish, and publish() PROMOTED the pocket's draft version to
# ``published`` AND claimed the canonical live deploy. So after arming + editing a
# site, get_draft(...) returned None (the draft was promoted away) and the
# ``published`` pointer had moved — which made request_publish_pocket() raise
# ("no draft version to publish") and the Submit-for-review UI 400.
#
# These tests assert the INTENDED Branch model: arming + editing a site builds a
# PREVIEW (draft) — it must NOT promote to published and must NOT move the live
# pointer — so the draft survives, the published pointer is unchanged, and
# request_publish_pocket() succeeds (creates the review Action). The live publish
# path (chat-create + approve→publish) is covered by test_component_edit.py /
# test_request_publish.py and must stay a real deploy.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.versions import service as versions

from pocketpaw.instinct.store import InstinctStore

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


class _FakeGenerator:
    """Records the source map + builder_origin it was asked to build so a test can
    assert the preview build still ran the smoke gate (a broken edit is caught)."""

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
    """Stand in for local_server.deploy_local so the PREVIEW serve does not need a
    real built dir on disk — returns the localhost URL the preview would be served
    at (what the builder iframe frames)."""
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


@pytest.fixture
def instinct_store(tmp_path, monkeypatch):
    """A throwaway InstinctStore wired into the accessor the sites service reads so
    request_publish_pocket() can actually create the review Action."""
    store = InstinctStore(tmp_path / "edit_draft_instinct.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda: store)
    return store


async def _make_svelte_pocket(workspace_id: str, user_id: str) -> str:
    """Persist a real svelte-engine Pocket via the pockets service and return its
    id — mirrors how create_svelte_site lands a pocket."""
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


async def test_arm_then_edit_leaves_a_reviewable_draft(beanie_test_db, instinct_store):
    """THE BUG: arm a svelte site editable → edit a component → the edit must leave
    a DRAFT (not promote it to published) and must NOT move the published pointer,
    so request_publish_pocket() succeeds (creates the review Action) instead of
    raising "no draft to publish".

    Before the fix this fails: make_site_editable() / edit_svelte_component() route
    through publish(), which promotes the draft → published, so get_draft() is None
    and request_publish_pocket() raises ValueError → the UI 400.
    """
    pocket_id = await _make_svelte_pocket("ws1", "u1")

    # Establish a published baseline so we can prove the EDIT does not move it.
    # (A real site has a live/published version; the edit must leave it alone.)
    # Snapshot the pocket's current source as the live version, the way a prior
    # publish would have — agent_create does not itself version the source.
    baseline = await versions.write_draft(
        scope_type="pocket",
        scope_id=pocket_id,
        workspace_id="ws1",
        content=dict(_SVELTE_SOURCE),
        author="u1",
    )
    await versions.publish(
        scope_type="pocket",
        scope_id=pocket_id,
        workspace_id="ws1",
        version_id=str(baseline.id),
    )
    published_before = await versions.get_published(scope_type="pocket", scope_id=pocket_id)
    assert published_before is not None
    assert published_before.id == baseline.id

    # 1. Arm the site for editing — must be a PREVIEW, not a live publish/promote.
    await sites_service.make_site_editable(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        _generator=_FakeGenerator(),
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )

    # 2. Edit a component — must persist a DRAFT + PREVIEW, not auto-publish.
    edit_gen, edit_cf = _FakeGenerator(), _FakeCF()
    await sites_service.edit_svelte_component(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/lib/components/Hero.svelte",
        new_source=_HERO_V2,
        _generator=edit_gen,
        _cloudflare=edit_cf,
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )

    # The preview build still ran (smoke-gate safety) and saw the edited source.
    assert edit_gen.built is not None
    assert edit_gen.built["source"]["src/lib/components/Hero.svelte"] == _HERO_V2

    # CORE ASSERTION 1 — the edit left a DRAFT (it was NOT promoted to published).
    draft = await versions.get_draft(scope_type="pocket", scope_id=pocket_id)
    assert draft is not None, "editing a site must leave a reviewable DRAFT, not auto-publish it"
    assert draft.content["src/lib/components/Hero.svelte"] == _HERO_V2

    # CORE ASSERTION 2 — the published pointer is UNCHANGED (the edit is not live).
    published_after = await versions.get_published(scope_type="pocket", scope_id=pocket_id)
    assert published_after is not None
    assert published_after.id == published_before.id, (
        "an edit must NOT move the published pointer — only an approved review does"
    )

    # CORE ASSERTION 3 — Submit-for-review now works (the bug's 400 is gone).
    action = await sites_service.request_publish_pocket(
        workspace_id="ws1", user_id="u1", pocket_id=pocket_id
    )
    assert action is not None
    blob = action.parameters["_artifact_change"]
    assert blob["to_version_id"] == str(draft.id)  # the draft is what we review
    assert blob["from_version_id"] == str(published_before.id)  # current live


async def test_make_editable_does_not_promote_or_go_live(beanie_test_db):
    """Arming a fresh (never-published) site for editing must build a PREVIEW only:
    it must NOT create a published version pointer and must NOT stamp a live Site
    deploy. The pocket stays a draft awaiting review."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    cf = _FakeCF()

    preview = await sites_service.make_site_editable(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        _generator=_FakeGenerator(),
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )

    # No published pointer — arming for edit is not a publish.
    published = await versions.get_published(scope_type="pocket", scope_id=pocket_id)
    assert published is None, "arming a site for editing must NOT promote to published"

    # A draft still exists (the working copy to review).
    draft = await versions.get_draft(scope_type="pocket", scope_id=pocket_id)
    assert draft is not None

    # The preview still returns a url the iframe can frame (builder_origin set so
    # the edit-bridge is injected), but it is a PREVIEW, not the canonical live
    # deploy: ``deployed`` is False (it is not live yet).
    assert preview.builder_origin  # editable → carries the bridge origin
    assert preview.deployed is False, "a preview must not claim a live deploy"


async def test_chat_create_publish_still_goes_live(beanie_test_db):
    """Backward-compat guard: a normal publish (no builder_origin — the chat-create
    / approve path) MUST still deploy live and promote the draft to published. The
    fix only changes the EDIT/arm path, never the live-publish path."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    cf = _FakeCF()

    site = await sites_service.publish_pocket(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        _generator=_FakeGenerator(),
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )

    # A real live publish: the worker was deployed and the Site doc is live.
    assert cf.put_calls == [site.script_name]
    assert site.deployed is True
    # The draft was promoted to published (this is what going live means).
    published = await versions.get_published(scope_type="pocket", scope_id=pocket_id)
    assert published is not None
