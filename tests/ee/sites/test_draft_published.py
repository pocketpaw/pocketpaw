# tests/ee/sites/test_draft_published.py — reproduces and locks the /sites
# draft-vs-published bug (pocketpaw#1345, "stop 'Live' from lying").
#
# The three reported failures, each asserted as a behaviour:
#   1. A freshly CREATED site is Draft, not Live (creating content must not stamp
#      deployed/live). Before the fix, the only create path (publish) stamped
#      deployed=True instantly.
#   2. "Live" is true only after a successful (mocked) deploy — and a FAILED
#      deploy leaves the site not-live (and the published pointer un-advanced).
#   3. The preview returns the current DRAFT content, not the dead published URL.
#   4. Refine writes a NEW draft version and leaves the published version's
#      content untouched until the next explicit publish.
#
# The generator + Cloudflare seams are injected with fakes (the same pattern as
# test_service.py), so no Bun/workerd/CF/network is touched.
#
# Created 2026-06-06 (feat/1345-draft-published).
# Updated 2026-06-06 (code review TEST-1 + TEST-2): added a svelte-track create →
# preview round-trip (create_draft_site(engine="svelte") → preview_content returns
# the SOURCE map, not None — the engine branch had zero coverage) and a
# failed-re-publish test on an ALREADY-LIVE site (refine v2 → publish v2 fails →
# the site must stay live on v1, published pointer unmoved — the existing
# failed-publish tests only covered the never-published case).
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.versions import service as versions_service
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.generator_client import SmokeGateFailed


class _FakeGenerator:
    """Successful build: returns a project dir, never raises."""

    def __init__(self) -> None:
        self.built: dict | None = None

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.built = kw
        return BuildResult(project_dir="/tmp/site", ripple_version="0.2.0")


class _FailingGenerator:
    """Build/smoke gate fails — the deploy must NOT proceed and Live must stay
    false (the 'Live only after a successful deploy' guarantee)."""

    async def build(self, **kw):
        raise SmokeGateFailed("workerd SSR failure: window is not defined")


class _FakeCF:
    def __init__(self) -> None:
        self.put_calls: list[str] = []

    async def put_worker(self, *, script_name, bundle):
        self.put_calls.append(script_name)
        return True


class _FailingCF:
    """Worker upload fails after a good build — Live must stay false."""

    async def put_worker(self, *, script_name, bundle):
        raise RuntimeError("cloudflare 500: dispatch namespace upload failed")


# ---------------------------------------------------------------------------
# 1. Fresh site → Draft, not Live
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_draft_site_is_draft_not_live(beanie_test_db):
    """Creating a site records its content as a DRAFT — it is NOT deployed and
    NOT live. This is the fix for 'a site is stamped deployed the moment it's
    created'."""
    site = await sites_service.create_draft_site(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="Fresh Site",
    )
    resp = sites_service._to_response(site)
    assert resp.status == "draft"
    assert resp.is_live is False
    assert site.deployed is False


# ---------------------------------------------------------------------------
# 2. Live only after a successful deploy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_sets_live_after_successful_deploy(beanie_test_db):
    """Publish promotes the draft to published, deploys, and flips Live true on a
    SUCCESSFUL deploy."""
    gen, cf = _FakeGenerator(), _FakeCF()
    site = await sites_service.create_draft_site(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="Site",
    )
    published = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="Site",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"export default {}",
    )
    resp = sites_service._to_response(published)
    assert resp.status == "published"
    assert resp.is_live is True
    assert published.deployed is True
    assert cf.put_calls == [published.script_name]
    # Same Site doc (publish reuses the draft site for this pocket, not a 2nd row).
    assert str(published.id) == str(site.id)


@pytest.mark.asyncio
async def test_publish_failed_build_leaves_site_not_live(beanie_test_db):
    """A failed build (smoke gate) must NOT mark the site live, must NOT advance
    the published pointer, and must leave a recoverable draft."""
    await sites_service.create_draft_site(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="Site",
    )
    with pytest.raises(SmokeGateFailed):
        await sites_service.publish(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            ripple_spec={"type": "container"},
            theme={},
            name="Site",
            _generator=_FailingGenerator(),
            _cloudflare=_FakeCF(),
            _bundle_reader=lambda d: b"x",
        )
    status = await sites_service.site_status(workspace_id="ws1", pocket_id="pk1")
    assert status.is_live is False
    assert status.status == "draft"


@pytest.mark.asyncio
async def test_publish_failed_deploy_leaves_site_not_live(beanie_test_db):
    """A good build but a failed Worker upload must NOT mark the site live."""
    await sites_service.create_draft_site(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container"},
        theme={},
        name="Site",
    )
    with pytest.raises(RuntimeError):
        await sites_service.publish(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            ripple_spec={"type": "container"},
            theme={},
            name="Site",
            _generator=_FakeGenerator(),
            _cloudflare=_FailingCF(),
            _bundle_reader=lambda d: b"x",
        )
    status = await sites_service.site_status(workspace_id="ws1", pocket_id="pk1")
    assert status.is_live is False


# ---------------------------------------------------------------------------
# 3. Preview returns the current draft content (no dead URL)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_returns_current_draft_content(beanie_test_db):
    """The builder preview renders the DRAFT content, not the published URL. After
    a publish + a refine, preview returns the NEW draft, while the published
    version keeps the old content."""
    gen, cf = _FakeGenerator(), _FakeCF()
    await sites_service.create_draft_site(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container", "rev": 1},
        theme={},
        name="Site",
    )
    await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container", "rev": 1},
        theme={},
        name="Site",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    # Refine: a new draft.
    await sites_service.record_site_draft(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        content={"type": "container", "rev": 2},
        engine="ripple",
    )
    preview = await sites_service.preview_content(workspace_id="ws1", pocket_id="pk1")
    assert preview == {"type": "container", "rev": 2}


# ---------------------------------------------------------------------------
# 4. Refine creates a new draft; published untouched until Publish
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refine_creates_new_draft_published_untouched(beanie_test_db):
    gen, cf = _FakeGenerator(), _FakeCF()
    await sites_service.create_draft_site(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container", "rev": 1},
        theme={},
        name="Site",
    )
    await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container", "rev": 1},
        theme={},
        name="Site",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    # Status right after publish: published, live.
    s1 = await sites_service.site_status(workspace_id="ws1", pocket_id="pk1")
    assert s1.status == "published"
    assert s1.is_live is True

    # Refine writes a new draft → status flips to draft (unpublished edits) but
    # the deploy/live state of the published version is unchanged until re-publish.
    await sites_service.record_site_draft(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        content={"type": "container", "rev": 2},
        engine="ripple",
    )
    s2 = await sites_service.site_status(workspace_id="ws1", pocket_id="pk1")
    assert s2.status == "draft"
    assert s2.draft_version == 2
    assert s2.published_version == 1


# ---------------------------------------------------------------------------
# Back-compat: the existing publish_pocket path still works and goes live.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_pocket_still_goes_live(beanie_test_db):
    """publish_pocket (the REST + MCP shared path) reads the pocket and publishes;
    a successful deploy still yields a live, published site."""
    from unittest.mock import AsyncMock, patch

    gen, cf = _FakeGenerator(), _FakeCF()
    wire = {"name": "Dental", "rippleSpec": {"type": "container", "theme": {}}}
    with patch(
        "pocketpaw_ee.cloud.pockets.service.get",
        new=AsyncMock(return_value=wire),
    ):
        site = await sites_service.publish_pocket(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            _generator=gen,
            _cloudflare=cf,
            _bundle_reader=lambda d: b"export default {}",
        )
    resp = sites_service._to_response(site)
    assert resp.status == "published"
    assert resp.is_live is True


# ---------------------------------------------------------------------------
# TEST-1: svelte-track create → preview round-trip. create_draft_site on the
# svelte engine stores the SOURCE map (content=None); preview_content must return
# that map, not None. If the engine branch were flipped this returns None.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_draft_svelte_site_preview_returns_source(beanie_test_db):
    source = {"src/routes/+page.svelte": "<h1>hi</h1>"}
    await sites_service.create_draft_site(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        theme={},
        name="Svelte Site",
        engine="svelte",
        source=source,
        ripple_spec=None,
    )
    preview = await sites_service.preview_content(workspace_id="ws1", pocket_id="pk1")
    assert preview == source  # the svelte source map, not None

    # The preview DTO also carries the svelte engine + source.
    dto = await sites_service.preview(workspace_id="ws1", pocket_id="pk1")
    assert dto.engine == "svelte"
    assert dto.content == source


# ---------------------------------------------------------------------------
# TEST-2: a FAILED re-publish on an already-live site must not take it down. The
# existing failed-publish tests only cover the never-published case; this covers
# the live-site case (publish v1 → refine v2 → publish v2 fails).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_republish_keeps_site_live_on_previous_version(beanie_test_db):
    """A site is live on v1. A refine creates draft v2. Publishing v2 fails at the
    build/deploy step. The site must STAY live on v1: deployed=True, the published
    pointer stays at v1 (v2 is NOT promoted), and the live URL is unchanged. The
    failed deploy must not promote v2 or take the site down."""
    gen, cf = _FakeGenerator(), _FakeCF()
    # Publish v1 → live.
    await sites_service.create_draft_site(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container", "rev": 1},
        theme={},
        name="Site",
    )
    live_v1 = await sites_service.publish(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        ripple_spec={"type": "container", "rev": 1},
        theme={},
        name="Site",
        _generator=gen,
        _cloudflare=cf,
        _bundle_reader=lambda d: b"x",
    )
    v1_url = live_v1.url
    s1 = await sites_service.site_status(workspace_id="ws1", pocket_id="pk1")
    assert s1.is_live is True
    assert s1.published_version == 1

    # Refine → draft v2 (live deploy untouched).
    await sites_service.record_site_draft(
        workspace_id="ws1",
        user_id="u1",
        pocket_id="pk1",
        content={"type": "container", "rev": 2},
        engine="ripple",
    )

    # Publish v2 fails at the build/smoke gate.
    with pytest.raises(SmokeGateFailed):
        await sites_service.publish(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="pk1",
            ripple_spec={"type": "container", "rev": 2},
            theme={},
            name="Site",
            _generator=_FailingGenerator(),
            _cloudflare=_FakeCF(),
            _bundle_reader=lambda d: b"x",
        )

    # The site is STILL live, STILL on v1.
    s2 = await sites_service.site_status(workspace_id="ws1", pocket_id="pk1")
    assert s2.is_live is True  # not taken down
    assert s2.published_version == 1  # v2 was NOT promoted
    assert s2.draft_version == 2  # the failed draft is still the working version
    assert s2.status == "draft"  # unpublished edits remain
    assert s2.url == v1_url  # the live URL did not change

    # v2 is still a draft in the log; the published version is still v1's content.
    pub = await versions_service.get_published(workspace_id="ws1", pocket_id="pk1")
    assert pub is not None
    assert pub.version_no == 1
    assert pub.content == {"type": "container", "rev": 1}

    # A failed deploy must NOT have uploaded a v2 worker.
    assert cf.put_calls == [live_v1.script_name]  # only the v1 publish uploaded
