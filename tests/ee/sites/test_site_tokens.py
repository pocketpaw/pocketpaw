# tests/ee/sites/test_site_tokens.py — the theme tokens file a site gets when it moves
# onto the ``static`` starter (``sites/site_tokens.py``): colours and fonts read from the
# site's own top-level rules (``var()`` resolved, ``@media`` and non-colour backgrounds
# ignored), the starter's defaults for the rest, no colour-scheme or dark block that
# would change how the page renders, and one idempotent ``<link>`` per page.
from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites import site_tokens  # noqa: E402

STARTER_TOKENS = """/* PLACEHOLDER values */
:root {
  color-scheme: light dark;

  --background: oklch(0.99 0 0);
  --foreground: oklch(0.18 0 0);
  --primary: oklch(0.25 0 0);
  --primary-foreground: oklch(0.98 0 0);
  --font-sans: system-ui, sans-serif;
  --font-heading: var(--font-sans);
  --radius: 0.625rem;
}

@media (prefers-color-scheme: dark) {
  :root {
    --background: oklch(0.16 0 0);
  }
}
"""


def _decls(css: str) -> dict[str, str]:
    return site_tokens.root_declarations(css)


def test_detects_colours_and_fonts_from_top_level_rules():
    source = {
        "index.html": "<html><head><style>h1, h2 { font-family: 'Fraunces', serif }</style>"
        "</head><body></body></html>",
        "styles.css": """
            :root { --ink: #1b1b1b; }
            body { background: #faf7f2; color: var(--ink); font-family: "Inter", sans-serif; }
            .button { background-color: rgb(200, 80, 40); }
            @media (prefers-color-scheme: dark) { body { background: #000; } }
        """,
    }
    assert site_tokens.detect_theme(source) == {
        "--background": "#faf7f2",
        "--foreground": "#1b1b1b",
        "--primary": "rgb(200, 80, 40)",
        "--font-sans": '"Inter", sans-serif',
        "--font-heading": "'Fraunces', serif",
    }


def test_a_link_colour_is_the_primary_when_there_is_no_button():
    source = {"a.css": "a { color: tomato } a:hover { color: red }"}
    assert site_tokens.detect_theme(source) == {"--primary": "tomato"}


def test_values_that_are_not_one_colour_are_skipped():
    source = {
        "s.css": "body { background: url(bg.png) #fff no-repeat; color: inherit; "
        "font-family: var(--missing); }"
    }
    assert site_tokens.detect_theme(source) == {}


def test_tokens_file_keeps_starter_defaults_and_drops_scheme_and_dark_block():
    css = site_tokens.tokens_css(STARTER_TOKENS, {"--background": "#faf7f2"})
    decls = _decls(css)
    assert decls["--background"] == "#faf7f2"
    assert decls["--foreground"] == "oklch(0.18 0 0)"
    assert decls["--radius"] == "0.625rem"
    assert "color-scheme" not in css
    assert "@media" not in css
    assert "PLACEHOLDER" not in css


def test_tokens_file_without_a_starter_still_has_every_role():
    decls = _decls(site_tokens.tokens_css(None, {}))
    for role in site_tokens.ROLES:
        assert decls.get(role), role


def test_link_goes_first_in_head_once():
    html = '<!doctype html><html><head><link rel="stylesheet" href="s.css"></head></html>'
    linked = site_tokens.link_tokens(html)
    assert linked.index(site_tokens.TOKENS_HREF) < linked.index("s.css")
    assert site_tokens.link_tokens(linked) == linked


@pytest.mark.parametrize(
    ("html", "before"),
    [
        ("<html><body><h1>x</h1></body></html>", "<body>"),
        ("<!DOCTYPE html>\n<h1>x</h1>", "<h1>"),
        ("<h1>x</h1>", "<h1>"),
    ],
)
def test_link_without_a_head(html, before):
    linked = site_tokens.link_tokens(html)
    assert linked.count(site_tokens.TOKENS_HREF) == 1
    assert linked.index(site_tokens.TOKENS_HREF) < linked.index(before)


def test_a_page_already_linking_tokens_is_left_alone():
    html = "<html><head><link href=./styles/tokens.css rel=stylesheet></head></html>"
    assert site_tokens.link_tokens(html) == html


def test_add_tokens_writes_the_file_and_links_every_page():
    source = {
        "index.html": "<html><head></head><body></body></html>",
        "about/index.html": "<html><head></head></html>",
        "styles.css": "body { color: #222 }",
    }
    out, added = site_tokens.add_tokens(source, STARTER_TOKENS)
    assert added == [site_tokens.TOKENS_REL]
    assert _decls(out[site_tokens.TOKENS_REL])["--foreground"] == "#222"
    assert site_tokens.TOKENS_HREF in out["index.html"]
    assert site_tokens.TOKENS_HREF in out["about/index.html"]
    assert out["styles.css"] == source["styles.css"]
    assert source["index.html"] == "<html><head></head><body></body></html>"  # not mutated


def test_add_tokens_never_overwrites_the_owner_file():
    source = {"index.html": "<html><head></head></html>", site_tokens.TOKENS_REL: ":root{}"}
    out, added = site_tokens.add_tokens(source, STARTER_TOKENS)
    assert added == []
    assert out[site_tokens.TOKENS_REL] == ":root{}"
    assert site_tokens.TOKENS_HREF in out["index.html"]
