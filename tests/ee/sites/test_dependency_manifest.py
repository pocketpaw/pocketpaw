# tests/ee/sites/test_dependency_manifest.py — ``paw.dependencies.json`` rules and the
# path-policy reservations that keep every edit lane away from it (PP-1).
#
# Created: 2026-09-24 (feat/sites-author-dependencies). The manifest is written only by
# the resolver, so the three path policies must refuse it in every spelling a path
# normalizer or a case-insensitive filesystem would collapse onto it. The svelte build
# shell also grew to the contract §2 set (vite.config.js, src/routes/+layout.ts/.js).

from __future__ import annotations

import json

import pytest
from pocketpaw_ee.sites import dependency_manifest as dm
from pocketpaw_ee.sites import html_paths, react_paths, svelte_paths

_SPELLINGS = [
    "paw.dependencies.json",
    "./paw.dependencies.json",
    "/paw.dependencies.json",
    "PAW.Dependencies.JSON",
    "src/../paw.dependencies.json",
    "src\\..\\paw.dependencies.json",
    "a/b/../../paw.dependencies.json",
]


@pytest.mark.parametrize("path", _SPELLINGS)
def test_every_spelling_is_the_manifest(path):
    assert dm.is_dependency_manifest_path(path)


@pytest.mark.parametrize(
    "path", ["src/paw.dependencies.json", "paw.dependencies.json.bak", "deps.json"]
)
def test_near_misses_are_not_the_manifest(path):
    assert not dm.is_dependency_manifest_path(path)


@pytest.mark.parametrize("path", _SPELLINGS)
def test_svelte_edit_lane_refuses_the_manifest(path):
    """Mutation: drop ``is_dependency_manifest_path`` from ``is_reserved_svelte_path``."""
    assert svelte_paths.is_reserved_svelte_path(path)
    reason = svelte_paths.svelte_path_rejection(path)
    assert reason is not None and "set_site_dependencies" in reason


@pytest.mark.parametrize("path", _SPELLINGS)
def test_react_edit_lane_refuses_the_manifest(path):
    assert react_paths.is_reserved_react_path(path)
    reason = react_paths.react_path_rejection(path)
    assert reason is not None and "set_site_dependencies" in reason


@pytest.mark.parametrize("path", _SPELLINGS)
def test_html_edit_lane_refuses_the_manifest(path):
    """The one engine whose root is authorable — without the reservation an html
    edit could write the file the generator turns into the page's importmap.
    Mutation: drop ``is_dependency_manifest_path`` from ``is_reserved_html_path``."""
    assert html_paths.is_reserved_html_path(path)
    reason = html_paths.html_path_rejection(path)
    assert reason is not None
    # An absolute spelling is refused first as an escape from the site dir, which
    # is the more fundamental problem; every other spelling names the right tool.
    if not path.startswith("/"):
        assert "set_site_dependencies" in reason


def test_html_root_files_stay_writable():
    assert html_paths.html_path_rejection("index.html") is None
    assert html_paths.html_path_rejection("deps.json") is None


@pytest.mark.parametrize(
    "path",
    [
        "package.json",
        "vite.config.ts",
        "vite.config.js",
        "svelte.config.js",
        "src/routes/+layout.ts",
        "src/routes/+layout.js",
        "./src/routes/+layout.ts",
        "src\\routes\\+layout.js",
        "src/lib/../routes/+layout.ts",
    ],
)
def test_the_svelte_build_shell_is_reserved(path):
    """Contract §2's svelte shell. A +layout.ts could switch prerendering off."""
    assert svelte_paths.is_reserved_svelte_path(path)
    assert svelte_paths.svelte_path_rejection(path) is not None


def test_svelte_layout_svelte_and_nested_layouts_stay_writable():
    assert svelte_paths.svelte_path_rejection("src/routes/+layout.svelte") is None
    assert svelte_paths.svelte_path_rejection("src/routes/about/+page.ts") is None


# ---------------------------------------------------------------------------
# parse / render / has_author_dependencies
# ---------------------------------------------------------------------------


def test_render_then_parse_round_trips():
    text = dm.render_manifest({"three": {"version": "0.170.0"}, "gsap": {"version": "3.12.5"}})
    data = json.loads(text)
    assert data["schema"] == 1
    assert list(data["packages"]) == ["gsap", "three"]  # sorted, stable
    assert dm.parse_manifest(text) == {
        "gsap": {"version": "3.12.5"},
        "three": {"version": "0.170.0"},
    }


@pytest.mark.parametrize("bad", ["{", "[]", '{"packages": []}', '{"packages": {"x": {}}}'])
def test_a_malformed_manifest_raises(bad):
    with pytest.raises(ValueError):
        dm.parse_manifest(bad)


def test_has_author_dependencies_fails_closed():
    """The sandbox-only guard keys on this. An unreadable manifest must count as
    declaring packages, or a broken file would unlock a host install.
    Mutation: return False on ValueError."""
    assert dm.has_author_dependencies({"paw.dependencies.json": "{not json"}) is True
    assert dm.has_author_dependencies(
        {"PAW.dependencies.json": dm.render_manifest({"x": {"version": "1.0.0"}})}
    )
    assert dm.has_author_dependencies({"paw.dependencies.json": dm.render_manifest({})}) is False
    assert dm.has_author_dependencies({"index.html": "<p>"}) is False
    assert dm.has_author_dependencies(None) is False


def test_author_packages_drops_ranges_and_bad_names():
    text = json.dumps(
        {
            "schema": 1,
            "packages": {
                "three": {"version": "0.170.0"},
                "floaty": {"version": "^1.0.0"},
                "Bad": {"version": "1.0.0"},
            },
        }
    )
    assert dm.author_packages({"paw.dependencies.json": text}) == {"three": "0.170.0"}


def test_the_toolchain_list_matches_the_contract():
    for name in (
        "svelte",
        "@sveltejs/kit",
        "vite",
        "react",
        "react-dom",
        "@vitejs/plugin-react",
        "tailwindcss",
        "@tailwindcss/vite",
        "@ripple-ui/svelte",
        "valibot",
        "@noble/hashes",
        "@cloudflare/workers-types",
    ):
        assert dm.is_toolchain_reserved(name), name
    for name in ("three", "gsap", "motion", "svelte-motion", "reactive", "@noble/curves"):
        assert not dm.is_toolchain_reserved(name), name


# ---------------------------------------------------------------------------
# Mirror of paw-sites PS-1's wider reserved set (case-insensitive everywhere)
# ---------------------------------------------------------------------------

_INSTALL_CONFIG = ["bunfig.toml", ".npmrc", "bun.lock", "bun.lockb", "package-lock.json"]
_VITE = [f"vite.config.{e}" for e in ("ts", "js", "mjs", "mts", "cjs", "cts")]


@pytest.mark.parametrize(
    "path", _INSTALL_CONFIG + _VITE + ["Package.JSON", "BUNFIG.TOML", ".\\.npmrc"]
)
def test_svelte_and_react_reserve_install_config_and_every_vite_spelling(path):
    """An authored bunfig/.npmrc/lockfile would change how the sandbox resolves —
    the release-age floor included. Mutation: drop ``*INSTALL_CONFIG_FILES``."""
    assert svelte_paths.is_reserved_svelte_path(path), path
    assert react_paths.is_reserved_react_path(path), path
    assert svelte_paths.svelte_path_rejection(path) is not None
    assert react_paths.react_path_rejection(path) is not None


@pytest.mark.parametrize(
    ("check", "path"),
    [
        (svelte_paths.is_reserved_svelte_path, "SRC/LIB/PAW/x.ts"),
        (svelte_paths.is_reserved_svelte_path, "src\\Lib\\Paw\\x.ts"),
        (svelte_paths.is_reserved_svelte_path, "src/Hooks.Server.ts"),
        (svelte_paths.is_reserved_svelte_path, "SRC/routes/+LAYOUT.ts"),
        (react_paths.is_reserved_react_path, "SRC/Paw/entry.tsx"),
        (react_paths.is_reserved_react_path, "Index.HTML"),
        (html_paths.is_reserved_html_path, "_PAW/edit-manifest.json"),
        (html_paths.is_reserved_html_path, "_Paw\\x"),
    ],
)
def test_reserved_namespaces_match_case_insensitively(check, path):
    assert check(path), path


def test_html_page_names_that_merely_share_a_prefix_stay_writable():
    assert html_paths.html_path_rejection("_pawprint.html") is None
    assert html_paths.html_path_rejection("_PAWPRINT.html") is None
    assert html_paths.html_path_rejection("bunfig.toml") is None  # html installs nothing
