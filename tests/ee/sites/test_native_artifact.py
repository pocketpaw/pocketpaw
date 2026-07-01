# tests/ee/sites/test_native_artifact.py — the native-artifact render path (NE-5b).
# Created 2026-07-01 (feat/native-editing-ne4b).
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
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
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
