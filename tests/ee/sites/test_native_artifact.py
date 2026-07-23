# tests/ee/sites/test_native_artifact.py — the native-artifact render path (NE-5b).
# Created 2026-07-01 (feat/native-editing-ne4b).
# Updated 2026-07-17 (fix/sites-prewarm-origin): the pre-warm must build with the SAME
# origin a browser VIEW resolves (its request Origin header) or the content hashes never
# match. New tests prove: a publish given an explicit prewarm_origin warms THAT origin
# (a view there HITs zero builds) even when the env fallback differs; a publish with no
# prewarm_origin keeps the env fallback (the defaulted-param no-request-origin path); and
# apply_leaf_edits forwards its prewarm_origin into the pre-warm's builder_origin.
# Updated 2026-07-02: + test_native_artifact_build_failure_maps_to_cloud_error — DEP-3
# hardening: a generator build failure (SmokeGateFailed / missing toolchain) now
# surfaces as a clean CloudError (sites.generator_failed), not an opaque 500, because
# get_native_artifact routes its build through _build_or_cloud_error.
# Updated 2026-07-17 (feat/sites-native-artifact-no-build): get_native_artifact is now a
# READ-THROUGH cache — a preview VIEW must not trigger a build. New tests prove:
#   * a repeat call with unchanged source is a cache HIT → ZERO extra builds;
#   * an injected _store hit skips the build entirely;
#   * a source change is a MISS → rebuild (the content hash tracks source);
#   * a LIVE svelte publish schedules a pre-warm that stores the armed artifact, so the
#     next native-artifact call is a HIT (DoD: publish stores → next preview is a hit);
#   * a leaf edit / component edit that changes source schedules a pre-warm; a rejected
#     leaf edit schedules none;
#   * the filesystem store degrades cleanly (missing dir / corrupt file read as a MISS)
#     and evicts to keep only current + previous.
#
# Under test: sites_service.get_native_artifact — the backend of the native
# shadow-render path. It ensures the pocket's ARMED svelte build (builder_origin set,
# so the generator stamps data-uid + embeds the paw-edit-manifest) and returns its
# <body> inner HTML + concatenated CSS as {pocket_id, body_html, css}. These use the
# shared ``beanie_test_db`` fixture (in-memory Mongo) so the pockets service persists
# a REAL svelte Pocket; the generator is faked at its ``build`` seam to return a
# hand-written built-output dir (a real .svelte-kit/cloudflare/index.html + a linked
# css file), so the REAL body/CSS extraction runs end-to-end WITHOUT Bun. They prove:
#   * happy path — returns {pocket_id, body_html, css}; body_html is the built
#     <body> INNER (data-uid leaf + paw-edit-manifest present, no <body>/<head>
#     wrapper); css concatenates the inline <style> + the linked stylesheet; and the
#     build was ARMED (builder_origin threaded, smoke=False) with the pocket source;
#   * no builder_origin → the configured PAW_SITES_BUILDER_ORIGIN fallback fires so
#     the armed build still gets a non-empty origin;
#   * the _read_built seam is injectable (a caller can bypass disk);
#   * a non-svelte (ripple) pocket → ValidationError (422) before any build;
#   * a missing / cross-tenant pocket → NotFound (404) from the pockets service.

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError, NotFound, ValidationError
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import service as sites_service

_HERO_V1 = "<section class='hero'><h1>Bright Smile</h1></section>"
_SVELTE_SOURCE = {
    "src/routes/+page.svelte": (
        "<script>import Hero from '$lib/components/Hero.svelte'</script><Hero/>"
    ),
    "src/lib/components/Hero.svelte": _HERO_V1,
    "src/app.css": ":root{--brand:#0A84FF}",
}

# A hand-written stand-in for the paw-sites ARMED build's prerendered index.html: the
# data-uid-stamped leaf + the embedded manifest live in <body>; the <head> links a
# stylesheet (relative ./_app href, as adapter-cloudflare emits) and inlines a
# critical <style>.
_ARMED_INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <link rel="stylesheet" href="./_app/immutable/assets/page.css" />
  <style>.inline-critical{margin:0}</style>
</head>
<body data-sveltekit-preload-data="hover">
  <section class="hero" data-paw-section="Hero">
    <h1 data-uid="Hero:headline:0">Bright Smile</h1>
  </section>
  <script id="paw-edit-manifest" type="application/json">{"leaves":["Hero:headline:0"]}</script>
</body>
</html>
"""
_LINKED_CSS = ".hero{color:#0A84FF}"


class _FakeGenerator:
    """Records the build kwargs and returns a BuildResult pointing at a pre-populated
    ``project_dir`` (a real .svelte-kit/cloudflare/ tree the test wrote), so the REAL
    ``_read_native_artifact`` extraction runs without Bun."""

    def __init__(self, project_dir: str) -> None:
        self.project_dir = project_dir
        self.built: dict | None = None

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.built = kw
        return BuildResult(project_dir=self.project_dir, ripple_version=None)


class _NoBuildGenerator:
    """A generator whose build MUST NOT run — used by the reject paths (a non-svelte
    or missing pocket must fail before any build is triggered)."""

    async def build(self, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("the build must not run on the reject path")


def _write_built_output(project_dir: Path) -> None:
    """Write a real armed build's static output under
    <project_dir>/.svelte-kit/cloudflare/ — index.html + the linked stylesheet —
    mirroring what a real bun build + adapter-cloudflare leave on disk."""
    cf = project_dir / ".svelte-kit" / "cloudflare"
    (cf / "_app" / "immutable" / "assets").mkdir(parents=True, exist_ok=True)
    (cf / "index.html").write_text(_ARMED_INDEX_HTML, encoding="utf-8")
    (cf / "_app" / "immutable" / "assets" / "page.css").write_text(_LINKED_CSS, encoding="utf-8")


async def _make_svelte_pocket(workspace_id: str, user_id: str) -> str:
    """Persist a real svelte-engine Pocket via the pockets service and return its id
    (mirrors how create_svelte_site lands a pocket)."""
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
async def test_native_artifact_returns_body_and_css(beanie_test_db, tmp_path):
    """A svelte pocket → {pocket_id, body_html, css}: body_html is the built <body>
    INNER (data-uid leaf + manifest, no <body>/<head> wrapper); css concatenates the
    inline <style> + the linked stylesheet; and the build was ARMED with the source."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    _write_built_output(tmp_path)
    gen = _FakeGenerator(str(tmp_path))

    result = await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin="https://dash.paw.example",
        _generator=gen,
    )

    assert result["pocket_id"] == pocket_id
    # body_html is the <body> INNER — the data-uid leaf + the manifest script, and
    # NOT the wrapping <body> tag / <head> chrome.
    assert 'data-uid="Hero:headline:0"' in result["body_html"]
    assert 'id="paw-edit-manifest"' in result["body_html"]
    assert "<body" not in result["body_html"]
    assert "<head" not in result["body_html"]
    # css concatenates the inline critical style AND the linked stylesheet.
    assert ".inline-critical{margin:0}" in result["css"]
    assert _LINKED_CSS in result["css"]
    # The build was ARMED (the resolved builder_origin threaded through) and carried
    # the pocket's svelte source on the svelte track, with the arm/preview smoke gate.
    assert gen.built is not None
    assert gen.built["builder_origin"] == "https://dash.paw.example"
    assert gen.built["engine"] == "svelte"
    assert gen.built["pocket_id"] == pocket_id
    assert gen.built["smoke"] is False
    assert gen.built["source"]["src/lib/components/Hero.svelte"] == _HERO_V1


@pytest.mark.asyncio
async def test_native_artifact_default_origin_from_config(beanie_test_db, tmp_path, monkeypatch):
    """With no builder_origin passed, the service falls back to the configured
    PAW_SITES_BUILDER_ORIGIN — the armed build still gets a non-empty origin so the
    generator stamps data-uid + the manifest."""
    monkeypatch.setenv("PAW_SITES_BUILDER_ORIGIN", "https://configured.paw.example")
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    _write_built_output(tmp_path)
    gen = _FakeGenerator(str(tmp_path))

    result = await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin="",
        _generator=gen,
    )

    assert result["pocket_id"] == pocket_id
    assert gen.built["builder_origin"] == "https://configured.paw.example"


@pytest.mark.asyncio
async def test_native_artifact_read_seam_is_injectable(beanie_test_db):
    """The _read_built seam is honored: the service returns whatever it yields, keyed
    on the build's project_dir — so a caller can bypass disk entirely."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    gen = _FakeGenerator("/tmp/paw-native-artifact-nonexistent")

    def _fake_read(project_dir: str) -> tuple[str, str]:
        assert project_dir == "/tmp/paw-native-artifact-nonexistent"
        return "<h1 data-uid='x:0'>hi</h1>", ".x{color:red}"

    result = await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin="https://dash.paw.example",
        _generator=gen,
        _read_built=_fake_read,
    )
    assert result == {
        "pocket_id": pocket_id,
        "body_html": "<h1 data-uid='x:0'>hi</h1>",
        "css": ".x{color:red}",
    }


@pytest.mark.asyncio
async def test_native_artifact_non_svelte_pocket_rejected(beanie_test_db):
    """A ripple-engine pocket has no svelte build — get_native_artifact raises
    ValidationError (422) BEFORE any build is triggered."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Ripple Pocket",
        type_="site",
        pattern="landing",
        ripple_spec={"type": "container"},
    )
    assert err is None, err

    with pytest.raises(ValidationError):
        await sites_service.get_native_artifact(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            builder_origin="https://dash.paw.example",
            _generator=_NoBuildGenerator(),
        )


@pytest.mark.asyncio
async def test_native_artifact_missing_pocket_raises_not_found(beanie_test_db):
    """A missing / cross-tenant pocket surfaces the pockets service's NotFound (404),
    before any build."""
    with pytest.raises(NotFound):
        await sites_service.get_native_artifact(
            workspace_id="ws1",
            user_id="u1",
            pocket_id="0123456789abcdef01234567",
            builder_origin="https://dash.paw.example",
            _generator=_NoBuildGenerator(),
        )


class _SmokeGateFailGenerator:
    """A generator whose build raises SmokeGateFailed — the failure a misconfigured
    toolchain / broken build produces. DEP-3 routes get_native_artifact's build
    through _build_or_cloud_error, so this must surface as a clean CloudError, not an
    opaque 500."""

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import SmokeGateFailed

        raise SmokeGateFailed("workerd SSR render failed")


@pytest.mark.asyncio
async def test_native_artifact_build_failure_maps_to_cloud_error(beanie_test_db):
    """DEP-3: a generator build failure (SmokeGateFailed / missing toolchain / non-zero
    build) is mapped to a clean CloudError (sites.generator_failed) instead of escaping
    as an opaque unhandled 500 — get_native_artifact routes its build through
    _build_or_cloud_error like the publish paths, NOT a bare generator.build()."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")

    with pytest.raises(CloudError) as excinfo:
        await sites_service.get_native_artifact(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            builder_origin="https://dash.paw.example",
            _generator=_SmokeGateFailGenerator(),
        )

    # A structured envelope with the DEP-3 machine code + a 5xx status — not a bare
    # RuntimeError / SmokeGateFailed escaping as an unhandled 500.
    assert excinfo.value.code == "sites.generator_failed"
    assert excinfo.value.status_code >= 500
    # SmokeGateFailed is a RuntimeError subclass, so confirm what surfaced is the
    # mapped CloudError, not the raw RuntimeError.
    assert not isinstance(excinfo.value, RuntimeError)


# ---------------------------------------------------------------------------
# feat/sites-native-artifact-no-build — read-through cache + background pre-warm.
# ---------------------------------------------------------------------------


class _CountingGenerator:
    """Records EACH build call so a test can assert the read-through cache skipped a
    rebuild. Returns a BuildResult pointing at a pre-populated project_dir."""

    def __init__(self, project_dir: str) -> None:
        self.project_dir = project_dir
        self.calls = 0

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.calls += 1
        return BuildResult(project_dir=self.project_dir, ripple_version=None)


class _MemoryArtifactStore:
    """In-memory ``_store`` seam — exercises the read-through logic without disk."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], tuple[str, str]] = {}
        self.writes = 0

    def read(self, pocket_id: str, content_hash: str) -> tuple[str, str] | None:
        return self.data.get((pocket_id, content_hash))

    def write(self, pocket_id: str, content_hash: str, body_html: str, css: str) -> None:
        self.data[(pocket_id, content_hash)] = (body_html, css)
        self.writes += 1


class _FakeCF:
    async def put_worker(self, *, script_name, bundle, bindings=None):  # noqa: ARG002
        return True


def _fake_local_deploy(site_id: str, project_dir: str) -> str:  # noqa: ARG001
    return f"http://127.0.0.1:9999/{site_id}/"


@pytest.mark.asyncio
async def test_native_artifact_repeat_call_is_cache_hit_zero_builds(beanie_test_db, tmp_path):
    """The core DoD: a repeat GET with UNCHANGED source performs ZERO subprocess builds.
    The first call is a MISS (build once, store); the second is a read-through HIT off
    the default filesystem store (redirected to tmp by the conftest fixture)."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    _write_built_output(tmp_path)
    gen = _CountingGenerator(str(tmp_path))

    r1 = await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin="https://dash.paw.example",
        _generator=gen,
    )
    r2 = await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin="https://dash.paw.example",
        _generator=gen,
    )

    assert r1 == r2
    assert gen.calls == 1, "the second identical view must be a cache HIT — zero rebuilds"


@pytest.mark.asyncio
async def test_native_artifact_store_hit_skips_build(beanie_test_db):
    """A store that already holds the content-hashed render serves it WITHOUT any build
    — proven by a generator whose build must never run."""
    from pocketpaw_ee.sites import generator_client

    pocket_id = await _make_svelte_pocket("ws1", "u1")
    store = _MemoryArtifactStore()
    wire = await pockets_service.get(pocket_id, "u1")
    content_hash = sites_service._artifact_content_hash(
        source=wire["source"],
        theme={},
        builder_origin="https://dash.paw.example",
        gen_version=generator_client.generator_version(),
    )
    store.data[(pocket_id, content_hash)] = ("<h1>cached</h1>", ".x{color:red}")

    result = await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin="https://dash.paw.example",
        _generator=_NoBuildGenerator(),
        _store=store,
    )

    assert result == {
        "pocket_id": pocket_id,
        "body_html": "<h1>cached</h1>",
        "css": ".x{color:red}",
    }
    assert store.writes == 0, "a cache hit must not re-store"


@pytest.mark.asyncio
async def test_native_artifact_source_change_is_cache_miss(beanie_test_db, tmp_path):
    """The content hash tracks the source: an unchanged view HITs, but a source mutation
    changes the hash → MISS → rebuild."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    _write_built_output(tmp_path)
    gen = _CountingGenerator(str(tmp_path))
    store = _MemoryArtifactStore()
    kw = dict(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin="https://dash.paw.example",
        _generator=gen,
        _store=store,
    )

    await sites_service.get_native_artifact(**kw)  # MISS → build 1
    await sites_service.get_native_artifact(**kw)  # HIT → still 1
    assert gen.calls == 1

    await pockets_service.set_svelte_source_file(
        pocket_id,
        "u1",
        component_path="src/lib/components/Hero.svelte",
        new_source="<section class='hero'><h1>Changed</h1></section>",
    )
    await sites_service.get_native_artifact(**kw)  # source changed → MISS → build 2
    assert gen.calls == 2, "a source change must be a cache MISS and rebuild"


@pytest.mark.asyncio
async def test_publish_prewarms_then_native_artifact_hits(
    beanie_test_db, tmp_path, monkeypatch, _captured_prewarms
):
    """DoD: a LIVE svelte publish stores an artifact (via the scheduled pre-warm) so the
    NEXT native-artifact view is a cache HIT — no on-view build."""
    # Force the LOCAL deploy branch so the live publish never shells out to a real
    # deployer (the workspace .env sets PAW_CF_DEPLOY_MODE=workers, which would route to
    # a real wrangler deploy); the fake local deploy makes doc.deployed=True.
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    _write_built_output(tmp_path)
    gen = _CountingGenerator(str(tmp_path))

    await sites_service.publish_pocket(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        _generator=gen,
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )

    # A live svelte publish scheduled exactly one background pre-warm.
    assert len(_captured_prewarms) == 1, "a live svelte publish must schedule a pre-warm"
    # Run it — the pre-warm builds the ARMED artifact and stores it (default fs store).
    await _captured_prewarms[0]

    # The next view is a read-through HIT — proven by a generator that must not build.
    result = await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        # No explicit origin → the same PAW_SITES_BUILDER_ORIGIN fallback the publish
        # pre-warm resolved, so the content hash matches and the store hits.
        builder_origin="",
        _generator=_NoBuildGenerator(),
    )
    assert result["pocket_id"] == pocket_id
    assert 'data-uid="Hero:headline:0"' in result["body_html"]
    assert 'id="paw-edit-manifest"' in result["body_html"]


@pytest.mark.asyncio
async def test_leaf_edit_change_schedules_prewarm(beanie_test_db, _captured_prewarms):
    """DoD: a leaf edit that CHANGES source schedules a background pre-warm (the seam is
    captured, not run)."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")

    async def _fake_apply(*, source, edits):
        new = dict(source)
        new["src/lib/components/Hero.svelte"] = "<section class='hero'><h1>Edited</h1></section>"
        return {"source": new, "results": [{"uid": edits[0]["uid"], "applied": True}]}

    await sites_service.apply_leaf_edits(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        edits=[{"uid": "Hero:headline:0", "op": {"kind": "text", "value": "Edited"}}],
        _apply=_fake_apply,
    )

    assert len(_captured_prewarms) == 1, "a source-changing leaf edit must schedule a pre-warm"


@pytest.mark.asyncio
async def test_leaf_edit_rejected_schedules_no_prewarm(beanie_test_db, _captured_prewarms):
    """A REJECTED leaf edit persists nothing, so it warms nothing — no pre-warm scheduled."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")

    async def _fake_apply(*, source, edits):
        # Byte-identical source back (a rejected edit) → the persist loop writes nothing.
        return {
            "source": dict(source),
            "results": [{"uid": edits[0]["uid"], "applied": False, "reason": "no unique match"}],
        }

    await sites_service.apply_leaf_edits(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        edits=[{"uid": "Hero:headline:0", "op": {"kind": "text", "value": "Edited"}}],
        _apply=_fake_apply,
    )

    assert len(_captured_prewarms) == 0, "a rejected edit changes nothing → no pre-warm"


@pytest.mark.asyncio
async def test_component_edit_schedules_prewarm(beanie_test_db, _captured_prewarms):
    """A targeted component edit (edit_svelte_component) also schedules a pre-warm after
    its preview republish succeeds."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    gen = _CountingGenerator("/tmp/paw-native-artifact-unused")

    await sites_service.edit_svelte_component(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        component_path="src/lib/components/Hero.svelte",
        new_source="<section class='hero'><h1>Edited</h1></section>",
        _generator=gen,
        _cloudflare=_FakeCF(),
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )

    assert len(_captured_prewarms) == 1, "a component edit must schedule a native-artifact pre-warm"


# ---------------------------------------------------------------------------
# fix/sites-prewarm-origin — the pre-warm must build with the SAME origin a browser
# VIEW resolves (its request Origin header), or the content hashes never match and the
# pre-warmed artifact is dead weight. publish_pocket / apply_leaf_edits take a
# ``prewarm_origin`` the REST routers thread the request Origin into.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_prewarm_uses_prewarm_origin_over_env(
    beanie_test_db, tmp_path, monkeypatch, _captured_prewarms
):
    """A live publish given an explicit ``prewarm_origin`` warms the artifact for THAT
    origin — the one a browser view resolves from its request Origin header — NOT the
    PAW_SITES_BUILDER_ORIGIN env fallback. Proven by setting the env to a DIFFERENT
    origin: with the pre-warm-origin bug, the pre-warm would warm at the env origin and
    the next view AT THE REQUEST ORIGIN would MISS — the _NoBuildGenerator would fire."""
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")
    # The env fallback is a DIFFERENT origin than the request origin: the OLD behaviour
    # warmed here, so a view at the request origin would have been a cold miss.
    monkeypatch.setenv("PAW_SITES_BUILDER_ORIGIN", "http://localhost:8888")
    view_origin = "https://paw.hzd.interacly.com"
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    _write_built_output(tmp_path)
    gen = _CountingGenerator(str(tmp_path))

    await sites_service.publish_pocket(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        prewarm_origin=view_origin,
        _generator=gen,
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )

    assert len(_captured_prewarms) == 1, "a live svelte publish must schedule a pre-warm"
    await _captured_prewarms[0]  # build + store the armed artifact at view_origin

    # The next view AT THE REQUEST ORIGIN is a read-through HIT (zero builds) — the
    # pre-warm used prewarm_origin, not the (different) env fallback.
    result = await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin=view_origin,
        _generator=_NoBuildGenerator(),
    )
    assert result["pocket_id"] == pocket_id
    assert 'data-uid="Hero:headline:0"' in result["body_html"]


@pytest.mark.asyncio
async def test_publish_no_prewarm_origin_keeps_env_fallback(
    beanie_test_db, tmp_path, monkeypatch, _captured_prewarms
):
    """A publish with NO prewarm_origin (a chat-agent / MCP publish — no request origin)
    keeps the pre-warm's PAW_SITES_BUILDER_ORIGIN env fallback, so a view with no origin
    (which resolves the same env fallback) still HITs. Guards the defaulted param: the
    fix must not regress the no-request-origin path."""
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "local")
    monkeypatch.setenv("PAW_SITES_BUILDER_ORIGIN", "https://configured.paw.example")
    pocket_id = await _make_svelte_pocket("ws1", "u1")
    _write_built_output(tmp_path)
    gen = _CountingGenerator(str(tmp_path))

    await sites_service.publish_pocket(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        _generator=gen,
        _bundle_reader=lambda d: b"export default {}",
        _local_deploy=_fake_local_deploy,
    )
    assert len(_captured_prewarms) == 1
    await _captured_prewarms[0]

    result = await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin="",  # resolves the same env fallback the pre-warm used
        _generator=_NoBuildGenerator(),
    )
    assert result["pocket_id"] == pocket_id


@pytest.mark.asyncio
async def test_leaf_edit_prewarm_uses_prewarm_origin(beanie_test_db, monkeypatch):
    """apply_leaf_edits forwards its ``prewarm_origin`` (the request Origin the
    leaf-edits router threads) into the native-artifact pre-warm's builder_origin, so
    the warmed artifact matches the origin the browser view asks for — not the env
    fallback. Captures the scheduling call to assert the threaded origin."""
    pocket_id = await _make_svelte_pocket("ws1", "u1")

    captured: dict = {}

    def _capture(**kw):
        captured.update(kw)

    monkeypatch.setattr(sites_service, "_schedule_native_prewarm", _capture)

    async def _fake_apply(*, source, edits):
        new = dict(source)
        new["src/lib/components/Hero.svelte"] = "<section class='hero'><h1>Edited</h1></section>"
        return {"source": new, "results": [{"uid": edits[0]["uid"], "applied": True}]}

    await sites_service.apply_leaf_edits(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        edits=[{"uid": "Hero:headline:0", "op": {"kind": "text", "value": "Edited"}}],
        prewarm_origin="https://paw.hzd.interacly.com",
        _apply=_fake_apply,
    )

    assert captured.get("builder_origin") == "https://paw.hzd.interacly.com", (
        "the leaf-edit pre-warm must build with the request origin, not the env fallback"
    )


def test_filesystem_store_missing_and_corrupt_read_as_miss(tmp_path, monkeypatch):
    """Local-mode degrade: the filesystem store treats a missing dir / file and a corrupt
    file as a MISS (returns None) so a bad cache entry degrades to a rebuild, never a 500."""
    from pocketpaw_ee.sites.generator_client import artifact_home

    monkeypatch.setenv("PAW_SITES_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    store = sites_service._FilesystemArtifactStore()

    # Nothing written yet → miss, no error.
    assert store.read("pocketX", "deadbeef") is None
    # Round-trips a write.
    store.write("pocketX", "deadbeef", "<b>hi</b>", ".a{}")
    assert store.read("pocketX", "deadbeef") == ("<b>hi</b>", ".a{}")
    # A corrupt file reads as a miss.
    corrupt = artifact_home() / "pocketY"
    corrupt.mkdir(parents=True, exist_ok=True)
    (corrupt / "abc123.json").write_text("{ not json", encoding="utf-8")
    assert store.read("pocketY", "abc123") is None


def test_filesystem_store_evicts_to_keep_cap(tmp_path, monkeypatch):
    """The store keeps only current + previous (PAW_SITES_ARTIFACT_KEEP) per pocket,
    evicting the oldest by mtime so it never grows unbounded."""
    import time

    from pocketpaw_ee.sites.generator_client import artifact_home

    monkeypatch.setenv("PAW_SITES_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("PAW_SITES_ARTIFACT_KEEP", "2")
    store = sites_service._FilesystemArtifactStore()

    for i in range(4):
        store.write("pocketZ", f"hash{i}", f"<b>{i}</b>", ".a{}")
        time.sleep(0.01)  # distinct mtimes so eviction order is deterministic

    files = list((artifact_home() / "pocketZ").glob("*.json"))
    assert len(files) == 2, "eviction keeps only current + previous"
    assert store.read("pocketZ", "hash3") is not None  # newest survives
    assert store.read("pocketZ", "hash2") is not None
    assert store.read("pocketZ", "hash0") is None  # oldest evicted
