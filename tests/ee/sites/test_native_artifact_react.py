# tests/ee/sites/test_native_artifact_react.py — the native-artifact path on the
# REACT track (RX-2). Created 2026-08-22.
#
# WHY A SEPARATE FILE. test_native_artifact.py is svelte end-to-end: its fixture
# writes a `.svelte-kit/cloudflare/` tree, and every assertion reads through it.
# React's build lands in `dist/` instead, so the two need different on-disk
# fixtures; forcing them into one file would mean parameterizing the output dir
# through helpers whose whole job is to BE the SvelteKit shape.
#
# WHAT THIS PINS. RX-1 shipped react with no edit lane, and `get_native_artifact`
# rejected any non-svelte pocket with a 422 before doing anything — which is what
# made the builder's Select tool dimmed on react sites. RX-2 widens the guard to
# every engine with a native edit lane, and the properties worth pinning are:
#
#   * a react pocket is ACCEPTED and its build is ARMED on the react track;
#   * the render is read from `dist/`, NOT the SvelteKit path — the bug this
#     replaces was a hardcoded `.svelte-kit/cloudflare` read;
#   * `html` and `ripple` are still REJECTED. Widening to "any source engine"
#     would have swept html in, and html has no build to render — it is selected
#     through its own srcdoc. The guard is about a BUILD, not about a source map,
#     so the rejection is as load-bearing as the acceptance;
#   * the artifact cache separates the two engines. The hash feeds a per-pocket
#     store, so this is belt-and-braces rather than a live collision — but an
#     engine-blind hash would serve a svelte render for a react pocket the moment
#     a pocket's engine ever changed under a stable id.

from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import service as sites_service

_REACT_SOURCE = {
    "src/App.tsx": (
        "import Hero from './components/Hero';\n"
        "export default function App() { return <main><Hero /></main>; }\n"
    ),
    "src/components/Hero.tsx": (
        "export default function Hero() {\n"
        "  return (\n"
        "    <section className=\"hero\">\n"
        "      <h1>Bright Smile</h1>\n"
        "    </section>\n"
        "  );\n"
        "}\n"
    ),
    "src/index.css": ":root{--brand:#0A84FF}",
}

# A stand-in for the react ARMED build's PRERENDERED dist/index.html: the prerender
# pass has already spliced the server-rendered markup into <div id="root">, so the
# stamped leaf and the manifest are real elements in the served body.
_ARMED_DIST_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <link rel="stylesheet" href="/assets/index.css" />
  <style>.inline-critical{margin:0}</style>
  <script id="paw-edit-manifest" type="application/json">[{"uid":"Hero:headline:1"}]</script>
</head>
<body>
  <div id="root"><section class="hero" data-paw-section="Hero">
    <h1 data-uid="Hero:headline:1">Bright Smile</h1>
  </section></div>
  <script id="paw-edit-bridge">/* gated */</script>
</body>
</html>
"""
_LINKED_CSS = ".hero{color:#0A84FF}"


class _FakeGenerator:
    """Records build kwargs and points at a pre-populated `dist/` tree so the REAL
    extraction runs without Bun or Vite."""

    def __init__(self, project_dir: str) -> None:
        self.project_dir = project_dir
        self.built: dict | None = None

    async def build(self, **kw):
        from pocketpaw_ee.sites.generator_client import BuildResult

        self.built = kw
        return BuildResult(project_dir=self.project_dir, ripple_version=None)


class _NoBuildGenerator:
    """A generator whose build MUST NOT run — the reject paths must fail first."""

    async def build(self, **kw):  # pragma: no cover - must not be reached
        raise AssertionError("the build must not run on the reject path")


def _write_react_dist(project_dir: Path) -> None:
    """Write a react build's static output under <project_dir>/dist/ — the shape
    `static_output_rel("react")` names and Vite actually emits."""
    dist = project_dir / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(_ARMED_DIST_HTML, encoding="utf-8")
    (dist / "assets" / "index.css").write_text(_LINKED_CSS, encoding="utf-8")


async def _make_pocket(engine: str, source, workspace_id="ws1", user_id="u1") -> str:
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id=workspace_id,
        owner_id=user_id,
        name="Bright Smile",
        type_="site",
        pattern="landing",
        ripple_spec=None,
        engine=engine,
        source=source,
        trusted=True,
    )
    assert err is None, err
    assert pocket_id is not None
    return pocket_id


@pytest.mark.asyncio
async def test_react_pocket_renders_from_dist(beanie_test_db, tmp_path):
    """A react pocket is accepted, armed on the REACT track, and read from dist/."""
    pocket_id = await _make_pocket("react", dict(_REACT_SOURCE))
    _write_react_dist(tmp_path)
    gen = _FakeGenerator(str(tmp_path))

    result = await sites_service.get_native_artifact(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        builder_origin="https://dash.paw.example",
        _generator=gen,
    )

    assert result["pocket_id"] == pocket_id
    # The prerendered, stamped markup came back as <body> INNER.
    assert 'data-uid="Hero:headline:1"' in result["body_html"]
    assert 'data-paw-section="Hero"' in result["body_html"]
    assert "<body" not in result["body_html"]
    # CSS concatenates the inline critical style and the linked stylesheet — proving
    # the read resolved dist/, not the SvelteKit path (which does not exist here).
    assert ".inline-critical{margin:0}" in result["css"]
    assert _LINKED_CSS in result["css"]
    # The build was ARMED, and on the react track.
    assert gen.built is not None
    assert gen.built["engine"] == "react"
    assert gen.built["builder_origin"] == "https://dash.paw.example"
    assert gen.built["smoke"] is False
    assert gen.built["source"]["src/App.tsx"] == _REACT_SOURCE["src/App.tsx"]


@pytest.mark.asyncio
async def test_html_pocket_still_rejected(beanie_test_db):
    """html is a SOURCE engine but has no build to render — its served artifact IS
    its source. Widening the guard to `is_source_engine` would have swept it in."""
    pocket_id = await _make_pocket("html", {"index.html": "<h1>hi</h1>"})

    with pytest.raises(ValidationError):
        await sites_service.get_native_artifact(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            _generator=_NoBuildGenerator(),
        )


@pytest.mark.asyncio
async def test_ripple_pocket_still_rejected(beanie_test_db):
    """ripple has no source map at all — rejected before any build, as before."""
    _view, pocket_id, err = await pockets_service.agent_create(
        workspace_id="ws1",
        owner_id="u1",
        name="Spec site",
        type_="site",
        pattern="landing",
        ripple_spec={"blocks": []},
        engine="ripple",
        source=None,
        trusted=True,
    )
    assert err is None, err

    with pytest.raises(ValidationError):
        await sites_service.get_native_artifact(
            workspace_id="ws1",
            user_id="u1",
            pocket_id=pocket_id,
            _generator=_NoBuildGenerator(),
        )


def test_content_hash_separates_engines():
    """The same source map builds to materially different HTML per engine, so the
    engine has to be part of the artifact's identity."""
    common = dict(
        source=dict(_REACT_SOURCE),
        theme={},
        builder_origin="https://dash.paw.example",
        gen_version="1.2.3",
    )
    svelte = sites_service._artifact_content_hash(**common, engine="svelte")
    react = sites_service._artifact_content_hash(**common, engine="react")
    assert svelte != react


def test_has_native_edit_lane_is_narrower_than_source_engine():
    """The predicate that gates this path: a BUILD to arm, not merely a source map."""
    from pocketpaw_ee.sites.engines import has_native_edit_lane, is_source_engine

    assert has_native_edit_lane("svelte") is True
    assert has_native_edit_lane("react") is True
    # html is a source engine but has no armed build — the distinction this exists for.
    assert is_source_engine("html") is True
    assert has_native_edit_lane("html") is False
    assert has_native_edit_lane("ripple") is False
