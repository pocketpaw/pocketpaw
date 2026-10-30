# tests/ee/sites/test_html_publish.py
# Created: 2026-07-10 (HE-3 — an html publish runs `generate` and nothing else).
# Proves the html publish path in GeneratorClient.build()/_build_one():
#   * html (needs_node_build == False) runs ONLY generate: `bun install`,
#     build_static (`bun run build`), and the workerd SSR smoke gate are NEVER
#     invoked. Asserted by spying on the fake runner's method calls — the same
#     subprocess seam the existing generator_client tests fake — and by proving the
#     ripple-dep rewrites (_rewrite_ripple_dep / _ripple_motion_dep) are never called
#     on the html path (an html site has no package.json).
#   * the html static-smoke check (_html_static_smoke) IS run and PASSES for good
#     html, and FAILS CLOSED (raises SmokeGateFailed, does NOT return a BuildResult)
#     for a malformed page or a broken internal link — so a bad page never reaches
#     the deploy step.
#   * REGRESSION: ripple + svelte (needs_node_build == True) still run
#     generate -> install -> build_static/smoke exactly as before — the skip is
#     gated on needs_node_build(engine), never on the smoke / static_build flags.
#   * html's STAGE-2 payload carries its {path: contents} source map on
#     `input.source` and OMITS rippleSpec (no binding split).
from __future__ import annotations

from pathlib import Path

import pytest
from pocketpaw_ee.sites import generator_client as gc
from pocketpaw_ee.sites.generator_client import (
    BuildResult,
    GeneratorClient,
    SmokeGateFailed,
    _html_static_smoke,
)

# asyncio_mode = "auto" (pyproject) drives the async tests; sync unit tests below
# run as plain sync — so no module-level pytest.mark.asyncio (it would warn on them).

_GOOD_HTML = (
    "<!doctype html>\n"
    "<html lang='en'>\n"
    "<head>\n"
    "  <meta charset='utf-8'>\n"
    "  <title>Bright Smile</title>\n"
    "  <link rel='stylesheet' href='style.css'>\n"
    "</head>\n"
    "<body>\n"
    "  <header><a href='#top'>Top</a> <a href='https://example.com'>ext</a></header>\n"
    "  <main><h1>Brighter Smiles</h1><img src='/assets/hero.png' alt='hero'><br></main>\n"
    "  <footer><a href='mailto:hi@example.com'>Email</a></footer>\n"
    "  <script src='app.js'></script>\n"
    "</body>\n"
    "</html>\n"
)


def _write_good_tree(root: Path) -> None:
    """Materialize a coherent static tree: index.html + every local asset it refs."""
    (root / "index.html").write_text(_GOOD_HTML, encoding="utf-8")
    (root / "style.css").write_text(":root{--brand:#0A84FF}", encoding="utf-8")
    (root / "app.js").write_text("console.log('hi')", encoding="utf-8")
    assets = root / "assets"
    assets.mkdir()
    (assets / "hero.png").write_bytes(b"\x89PNG\r\n")


class _SpyRunner:
    """Fake runner recording which subprocess steps build() drove, and returning a
    projectDir pointing at a real on-disk static tree so the html smoke can run.

    Records ``calls`` so a test can assert install / build_static / smoke were (or
    were NOT) invoked. Mirrors the real runner's full method set (generate, install,
    build_static, smoke, install_inputs_hash) so the ripple/svelte regression runs
    the modern build_static path, not the legacy smoke() fallback."""

    def __init__(self, project_dir: str) -> None:
        self._project_dir = project_dir
        self.calls: list[str] = []

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.calls.append("generate")
        return {"projectDir": self._project_dir, "engine": input_json.get("engine")}

    def install_inputs_hash(self, project_dir: str) -> str:
        # A stable hash: irrelevant on the html path (install is skipped entirely);
        # on the ripple/svelte regression the first build always installs (no prior
        # sentinel), which is what we assert.
        return "h1"

    async def install(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("install")
        return True, "ok"

    async def build_static(self, project_dir: str, *, gate: bool) -> tuple[bool, str]:
        self.calls.append(f"build_static(gate={gate})")
        return True, "ok"

    async def smoke(self, project_dir: str) -> tuple[bool, str]:
        self.calls.append("smoke")
        return True, "ok"


class _CapturingSpyRunner(_SpyRunner):
    """_SpyRunner that also captures the input_json handed to generate()."""

    def __init__(self, project_dir: str) -> None:
        super().__init__(project_dir)
        self.input_json: dict | None = None

    async def generate(self, input_json: dict, out_dir: str) -> dict:
        self.input_json = input_json
        return await super().generate(input_json, out_dir)


def _html_kwargs(source: dict[str, str]) -> dict:
    return dict(
        engine="html",
        source=source,
        ripple_spec=None,
        theme={"primary": "#0A84FF"},
        site_id="site_html",
        title="Bright Smile",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
    )


# --------------------------------------------------------------------------- #
# The headline: an html publish runs generate + the html smoke, NOTHING else.
# --------------------------------------------------------------------------- #


async def test_html_publish_runs_only_generate_and_html_smoke(tmp_path, monkeypatch):
    """HE-3 core: for engine='html' build() runs generate + the html static smoke and
    SKIPS install, build_static, and the workerd smoke gate — asserted on the spy's
    recorded calls (the subprocess seam)."""
    _write_good_tree(tmp_path)

    # Guard: if the html path ever reached the package.json rewrites, this explodes.
    def _boom_rewrite(*a, **k):
        raise AssertionError("_rewrite_ripple_dep must NEVER run on the html path")

    def _boom_motion(*a, **k):
        raise AssertionError("_ripple_motion_dep must NEVER run on the html path")

    monkeypatch.setattr(gc, "_rewrite_ripple_dep", _boom_rewrite)
    monkeypatch.setattr(gc, "_ripple_motion_dep", _boom_motion)

    smoke_dirs: list[str] = []
    real_smoke = gc._html_static_smoke

    def _spy_smoke(static_dir: Path, **kw):
        smoke_dirs.append(str(static_dir))
        return real_smoke(static_dir, **kw)

    monkeypatch.setattr(gc, "_html_static_smoke", _spy_smoke)

    runner = _SpyRunner(str(tmp_path))
    client = GeneratorClient(_runner=runner)
    result = await client.build(**_html_kwargs({"index.html": _GOOD_HTML}))

    # Only generate ran on the runner — no install / build_static / smoke.
    assert runner.calls == ["generate"]
    assert not any(c.startswith("install") for c in runner.calls)
    assert not any(c.startswith("build_static") for c in runner.calls)
    assert "smoke" not in runner.calls
    # The html static smoke WAS invoked (and passed), against the project dir.
    assert smoke_dirs == [str(tmp_path)]
    assert isinstance(result, BuildResult)
    assert result.project_dir == str(tmp_path)


async def test_html_publish_sends_source_not_ripple_spec(tmp_path):
    """html's STAGE-2 payload carries its {path: contents} map on input.source and
    OMITS rippleSpec (no binding split — dynamic html is a non-goal)."""
    _write_good_tree(tmp_path)
    runner = _CapturingSpyRunner(str(tmp_path))
    client = GeneratorClient(_runner=runner)
    src = {"index.html": _GOOD_HTML, "style.css": ":root{}"}
    await client.build(**_html_kwargs(src))

    sent = runner.input_json
    assert sent is not None
    assert sent["engine"] == "html"
    assert sent["source"] == src
    assert "rippleSpec" not in sent
    # No svelte binding split happened on the html source map.
    for k in ("objects", "sources", "actions", "auth"):
        assert k not in sent


async def test_html_publish_malformed_fails_closed_before_deploy(tmp_path, monkeypatch):
    """A malformed page (an unbalanced/stray end tag) FAILS the html smoke: build()
    raises SmokeGateFailed and returns NO BuildResult, so it never reaches deploy.
    The runner still only ran generate — the skip happened, the smoke bit."""
    (tmp_path / "index.html").write_text(
        "<html><body><div><h1>oops</h1></span></div></body></html>", encoding="utf-8"
    )
    runner = _SpyRunner(str(tmp_path))
    client = GeneratorClient(_runner=runner)
    with pytest.raises(SmokeGateFailed) as exc:
        await client.build(**_html_kwargs({"index.html": "<html>...</html>"}))
    assert "html smoke" in str(exc.value)
    # Only generate ran — install/build_static/smoke were skipped, and the bad page
    # was rejected before any deploy step.
    assert runner.calls == ["generate"]


async def test_html_publish_broken_internal_link_fails_closed(tmp_path):
    """A broken internal link (href to a file that isn't in the output tree) FAILS
    the html smoke, so a page with a dead local asset never deploys."""
    (tmp_path / "index.html").write_text(
        "<html><head><link rel='stylesheet' href='missing.css'></head>"
        "<body><h1>hi</h1></body></html>",
        encoding="utf-8",
    )
    runner = _SpyRunner(str(tmp_path))
    client = GeneratorClient(_runner=runner)
    with pytest.raises(SmokeGateFailed) as exc:
        await client.build(**_html_kwargs({"index.html": "<html>...</html>"}))
    assert "does not resolve" in str(exc.value)
    assert runner.calls == ["generate"]


# --------------------------------------------------------------------------- #
# Regression: ripple + svelte still run the FULL build chain (unchanged).
# --------------------------------------------------------------------------- #


async def test_ripple_publish_still_runs_full_build_chain(tmp_path):
    """needs_node_build('ripple') is True — the ripple path is UNCHANGED: generate ->
    install -> build_static(gate=True). The html skip must not touch it."""
    runner = _SpyRunner(str(tmp_path))
    client = GeneratorClient(_runner=runner)
    await client.build(
        ripple_spec={"type": "container"},
        theme={"primary": "#0A84FF"},
        site_id="site_rp",
        title="Dashboard",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_y",
    )
    assert runner.calls == ["generate", "install", "build_static(gate=True)"]


async def test_svelte_publish_still_runs_full_build_chain(tmp_path):
    """needs_node_build('svelte') is True — the svelte path is UNCHANGED: generate ->
    install -> build_static(gate=True)."""
    runner = _SpyRunner(str(tmp_path))
    client = GeneratorClient(_runner=runner)
    await client.build(
        engine="svelte",
        source={"src/routes/+page.svelte": "<h1>hi</h1>"},
        ripple_spec=None,
        theme={},
        site_id="site_sv",
        title="Tally",
        capture_api_base="https://api.paw.example",
        capture_signed_key="pp_tok_x",
    )
    assert runner.calls == ["generate", "install", "build_static(gate=True)"]


async def test_html_skip_is_independent_of_smoke_and_static_build_flags(tmp_path):
    """The skip is gated on needs_node_build(engine), NOT on smoke / static_build.
    Even with the default flags (smoke=True, static_build=True) — the exact flags a
    LIVE ripple/svelte publish uses — an html build still skips install/build_static.
    This proves the flags were not overloaded to carry the skip."""
    _write_good_tree(tmp_path)
    runner = _SpyRunner(str(tmp_path))
    client = GeneratorClient(_runner=runner)
    await client.build(smoke=True, static_build=True, **_html_kwargs({"index.html": _GOOD_HTML}))
    assert runner.calls == ["generate"]


# --------------------------------------------------------------------------- #
# _html_static_smoke unit coverage (the check in isolation).
# --------------------------------------------------------------------------- #


def test_html_smoke_passes_good_tree(tmp_path):
    _write_good_tree(tmp_path)
    _html_static_smoke(tmp_path)  # no raise


def test_html_smoke_missing_index_fails(tmp_path):
    with pytest.raises(SmokeGateFailed) as exc:
        _html_static_smoke(tmp_path)
    assert "no index.html" in str(exc.value)


def test_html_smoke_no_elements_fails(tmp_path):
    (tmp_path / "index.html").write_text("just plain text, no tags", encoding="utf-8")
    with pytest.raises(SmokeGateFailed) as exc:
        _html_static_smoke(tmp_path)
    assert "no HTML elements" in str(exc.value)


def test_html_smoke_stray_end_tag_fails(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><body><p>hi</p></div></body></html>", encoding="utf-8"
    )
    with pytest.raises(SmokeGateFailed) as exc:
        _html_static_smoke(tmp_path)
    assert "unbalanced" in str(exc.value)


def test_html_smoke_broken_link_fails(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><body><img src='nope.png'></body></html>", encoding="utf-8"
    )
    with pytest.raises(SmokeGateFailed) as exc:
        _html_static_smoke(tmp_path)
    assert "does not resolve" in str(exc.value)


def test_html_smoke_traversal_ref_fails(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><body><img src='../secret.png'></body></html>", encoding="utf-8"
    )
    with pytest.raises(SmokeGateFailed) as exc:
        _html_static_smoke(tmp_path)
    assert "escapes the site root" in str(exc.value)


def test_html_smoke_ignores_external_and_anchor_refs(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><body>"
        "<a href='https://example.com'>x</a>"
        "<a href='mailto:hi@example.com'>m</a>"
        "<a href='tel:+15551234'>t</a>"
        "<a href='//cdn.example.com/x.js'>p</a>"
        "<a href='#section'>a</a>"
        "<a href='?q=1'>q</a>"
        "<h1>ok</h1>"
        "</body></html>",
        encoding="utf-8",
    )
    _html_static_smoke(tmp_path)  # external / anchor / same-doc refs are not checked


def test_html_smoke_site_absolute_and_query_refs_resolve(tmp_path):
    (tmp_path / "index.html").write_text(
        "<html><head><link rel='stylesheet' href='/style.css?v=2'></head>"
        "<body><h1>hi</h1></body></html>",
        encoding="utf-8",
    )
    (tmp_path / "style.css").write_text(":root{}", encoding="utf-8")
    _html_static_smoke(tmp_path)  # /style.css resolves at root; ?v=2 is stripped
