# tests/ee/sites/test_site_token_rewrite.py — a site moving onto the static starter
# gets its own CSS pointed at the theme tokens (``sites/site_token_rewrite.py``, run by
# ``site_tokens.add_tokens``): each literal the tokens file was derived from becomes
# ``var(--token)`` in colour and font-family declarations of top-level rules, across
# .css files and inline <style> blocks, with equivalent colour forms matched. Left
# alone: @media / other at-rule blocks, url() contents, vendor and library CSS, the
# tokens file itself, and any role the site already declares as a custom property.
# The rewrite is idempotent, and the resolved value of every declaration is unchanged,
# so the page renders exactly as before.
from __future__ import annotations

import re

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites import site_token_rewrite, site_tokens  # noqa: E402

THEME = {
    "--background": "#faf7f2",
    "--foreground": "#1b1b1b",
    "--primary": "rgb(200, 80, 40)",
    "--font-sans": '"Inter", sans-serif',
    "--font-heading": "'Fraunces', serif",
}


def _rw(css: str, theme: dict[str, str] | None = None) -> str:
    return site_token_rewrite.rewrite_css(css, THEME if theme is None else theme)


# ---------------------------------------------------------------------------
# What gets rewritten
# ---------------------------------------------------------------------------


def test_rewrites_each_role_in_its_property_contexts():
    css = (
        'body { background: #faf7f2; color: #1b1b1b; font-family: "Inter", sans-serif; }\n'
        "h1, h2 { font-family: 'Fraunces', serif }\n"
        ".btn { background-color: rgb(200, 80, 40); border-color: rgb(200, 80, 40) }\n"
        "a { color: rgb(200, 80, 40) }\n"
        "svg.icon { fill: #1b1b1b; stroke: rgb(200, 80, 40) }\n"
    )
    assert _rw(css) == (
        "body { background: var(--background); color: var(--foreground); "
        "font-family: var(--font-sans); }\n"
        "h1, h2 { font-family: var(--font-heading) }\n"
        ".btn { background-color: var(--primary); border-color: var(--primary) }\n"
        "a { color: var(--primary) }\n"
        "svg.icon { fill: var(--foreground); stroke: var(--primary) }\n"
    )


@pytest.mark.parametrize(
    "literal",
    ["#FAF7F2", "#faf7f2", "rgb(250, 247, 242)", "rgb(250 247 242)", "rgba(250,247,242,1)"],
)
def test_equivalent_colour_forms_match(literal):
    assert _rw(f".x {{ background: {literal} }}") == ".x { background: var(--background) }"


def test_short_hex_matches_its_long_form():
    theme = {"--foreground": "#222222"}
    assert _rw("p { color: #222 }", theme) == "p { color: var(--foreground) }"


@pytest.mark.parametrize(
    "literal", ["#faf7f3", "rgba(250, 247, 242, 0.5)", "#faf7f280", "hsl(36 50% 97%)"]
)
def test_a_different_colour_is_left_alone(literal):
    css = f".x {{ background: {literal} }}"
    assert _rw(css) == css


def test_a_colour_inside_a_shorthand_or_gradient_is_replaced_in_place():
    css = ".x { background: linear-gradient(#faf7f2, #fff) no-repeat; border: 1px solid red }"
    assert _rw(css) == (
        ".x { background: linear-gradient(var(--background), #fff) no-repeat; "
        "border: 1px solid red }"
    )


def test_important_and_spacing_survive():
    css = "a{color:RGB(200,80,40) !important}"
    assert _rw(css) == "a{color:var(--primary) !important}"


def test_a_heading_rule_prefers_the_heading_font_when_both_fonts_match():
    theme = {"--font-sans": "Inter, sans-serif", "--font-heading": "Inter, sans-serif"}
    css = "body { font-family: Inter, sans-serif } h2 { font-family: 'Inter',sans-serif }"
    assert _rw(css, theme) == (
        "body { font-family: var(--font-sans) } h2 { font-family: var(--font-heading) }"
    )


def test_the_sites_own_custom_properties_point_at_the_tokens():
    css = ":root { --ink: #1B1B1B; --paper: #faf7f2; --display: 'Fraunces', serif; --gap: 1rem }"
    assert _rw(css) == (
        ":root { --ink: var(--foreground); --paper: var(--background); "
        "--display: var(--font-heading); --gap: 1rem }"
    )


def test_inline_style_blocks_are_rewritten_too():
    source = {
        "index.html": "<html><head><style>body { color: #1b1b1b }</style></head></html>",
        "print.html": "<style media=print>body { color: #1b1b1b }</style>",
    }
    out = site_token_rewrite.rewrite_source(source, THEME)
    assert "<style>body { color: var(--foreground) }</style>" in out["index.html"]
    assert out["print.html"] == source["print.html"]


# ---------------------------------------------------------------------------
# What is left alone
# ---------------------------------------------------------------------------


def test_at_rule_blocks_are_never_touched():
    css = (
        "@media (prefers-color-scheme: dark) { body { background: #faf7f2 } }\n"
        "@supports (color: red) { a { color: rgb(200, 80, 40) } }\n"
        '@font-face { font-family: "Inter", sans-serif; src: url(i.woff2) }\n'
    )
    assert _rw(css) == css


def test_url_contents_and_strings_are_never_touched():
    css = (
        ".a { background: url(#faf7f2) #faf7f2 } .b { fill: url('#1b1b1b') } "
        '.c { background: url("data:image/svg+xml;fill=%23faf7f2") }'
    )
    assert _rw(css) == (
        ".a { background: url(#faf7f2) var(--background) } .b { fill: url('#1b1b1b') } "
        '.c { background: url("data:image/svg+xml;fill=%23faf7f2") }'
    )


def test_other_properties_and_custom_names_are_left_alone():
    css = ".x { box-shadow: 0 0 2px #1b1b1b; outline-color: #1b1b1b; --brand-red: x; }"
    assert _rw(css) == css


def test_comments_are_kept_and_a_commented_declaration_is_skipped():
    css = "/* body { color: #1b1b1b } */ p { color: /* ink */ #1b1b1b; background: #faf7f2 }"
    assert _rw(css) == (
        "/* body { color: #1b1b1b } */ p { color: /* ink */ #1b1b1b; "
        "background: var(--background) }"
    )


@pytest.mark.parametrize(
    "path",
    [
        "vendor/site.css",
        "assets/node_modules/x/a.css",
        "css/bootstrap.min.css",
        "css/font-awesome.css",
        "fontawesome/all.css",
        "styles/tailwind.css",
        "lib/swiper-bundle.min.css",
    ],
)
def test_vendor_and_library_css_is_skipped(path):
    source = {path: "body { color: #1b1b1b }"}
    assert site_token_rewrite.rewrite_source(source, THEME) == source


def test_library_css_is_known_by_its_banner():
    source = {"css/main.css": "/*! tailwindcss v3.4.1 | MIT License */\nbody{color:#1b1b1b}"}
    assert site_token_rewrite.rewrite_source(source, THEME) == source


def test_the_tokens_file_itself_is_skipped():
    tokens = ":root { --background: #faf7f2; --foreground: #1b1b1b; }"
    source = {site_tokens.TOKENS_REL: tokens}
    assert site_token_rewrite.rewrite_source(source, THEME) == source


def test_a_role_the_site_declares_itself_is_skipped():
    """The site's own ``--primary`` would shadow the token, so pointing a rule at it
    could change the page. That role stays literal; the others are rewritten."""
    source = {
        "a.css": ":root { --primary: blue } .btn { background: rgb(200,80,40); color: #1b1b1b }"
    }
    out = site_token_rewrite.rewrite_source(source, THEME)
    assert out["a.css"] == (
        ":root { --primary: blue } .btn { background: rgb(200,80,40); color: var(--foreground) }"
    )


def test_source_is_not_mutated_and_binary_values_pass_through():
    source = {"a.css": "p{color:#1b1b1b}", "logo.png": b"\x89PNG"}
    out = site_token_rewrite.rewrite_source(source, THEME)
    assert source["a.css"] == "p{color:#1b1b1b}"
    assert out["logo.png"] == b"\x89PNG"


# ---------------------------------------------------------------------------
# Idempotent, and rendering is unchanged
# ---------------------------------------------------------------------------

FIXTURE = {
    "index.html": (
        "<!doctype html><html><head><link rel=stylesheet href=site.css>"
        "<style>h1, h2 { font-family: 'Fraunces', serif; color: #1B1B1B }"
        " .hero { background: #FAF7F2 url(hero.jpg) }</style></head><body></body></html>"
    ),
    "site.css": (
        ":root { --ink: #1b1b1b; --accent: rgb(200 80 40) }\n"
        "body { background-color: #faf7f2; color: var(--ink); "
        'font-family: "Inter", sans-serif }\n'
        ".btn { background: rgb(200, 80, 40); color: #fff; border-color: #c85028 }\n"
        "a:hover { color: var(--accent) }\n"
        "footer { background: #1b1b1b; color: #faf7f2 }\n"
        "@media (max-width: 600px) { body { background: #faf7f2 } }\n"
    ),
}


def test_running_it_twice_changes_nothing_the_second_time():
    once, _ = site_tokens.add_tokens(FIXTURE, None)
    twice, added = site_tokens.add_tokens(once, None)
    assert added == []
    assert twice == once


_HEX = re.compile(r"#([0-9a-f]{6}|[0-9a-f]{3})(?![0-9a-f])")
_RGB = re.compile(r"rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)\s*\)")


def _canon(value: str) -> str:
    """Test-local normal form: every hex / rgb() colour as ``rgb(r,g,b)``, quotes and
    whitespace evened out. Independent of the code under test."""
    value = value.strip().lower().replace('"', "'")

    def hexrgb(m: re.Match) -> str:
        h = m.group(1)
        h = "".join(c * 2 for c in h) if len(h) == 3 else h
        return f"rgb({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)})"

    value = _HEX.sub(hexrgb, value)
    value = _RGB.sub(lambda m: f"rgb({','.join(str(int(float(g))) for g in m.groups())})", value)
    return re.sub(r"\s*,\s*", ",", re.sub(r"\s+", " ", value))


def _resolve(value: str, props: dict[str, str]) -> str:
    for _ in range(10):
        value = re.sub(r"var\((--[-\w]+)\)", lambda m: props[m.group(1)], value)
    return value


def _sheets(source: dict) -> list[str]:
    sheets = []
    for path, text in source.items():
        if path.endswith(".css") and path != site_tokens.TOKENS_REL:
            sheets.append(text)
        elif path.endswith(".html"):
            sheets.extend(re.findall(r"<style[^>]*>(.*?)</style>", text, re.S))
    return sheets


def _computed(source: dict) -> list[tuple[str, str, str]]:
    """Every top-level declaration as ``(selector, property, value)`` with ``var()``
    resolved through the tokens file and the site's own custom properties, the way the
    browser substitutes them, plus every at-rule block verbatim."""
    props = site_tokens.root_declarations(source.get(site_tokens.TOKENS_REL))
    out: list[tuple[str, str, str]] = []
    for sheet in _sheets(source):
        for selector, body in site_tokens._top_level_rules(sheet):
            if selector.startswith("@"):
                continue
            for name, value in re.findall(r"([-\w]+)\s*:\s*([^;]+)", body):
                if name.startswith("--"):
                    props[name] = value.strip()
        out.extend(("@", "block", m) for m in re.findall(r"@media[^{]*\{.*\}\s*\}", sheet, re.S))
    for sheet in _sheets(source):
        for selector, body in site_tokens._top_level_rules(sheet):
            for name, value in re.findall(r"([-\w]+)\s*:\s*([^;]+)", body):
                out.append((selector, name, _canon(_resolve(value, props))))
    return out


def test_the_rewrite_leaves_every_computed_value_unchanged():
    """The pixel-identity guard: tokens.css declares exactly the replaced literals, so
    resolving ``var()`` gives back the value each declaration had before."""
    linked, _ = site_tokens.add_tokens(FIXTURE, None)
    # Same page, tokens linked but CSS as authored: the "before" render.
    before = {**FIXTURE, site_tokens.TOKENS_REL: linked[site_tokens.TOKENS_REL]}
    assert linked["site.css"] != FIXTURE["site.css"]  # something was rewritten
    assert "var(--background)" in linked["site.css"]
    assert "var(--font-heading)" in linked["index.html"]
    assert _computed(linked) == _computed(before)


# ---------------------------------------------------------------------------
# Through add_tokens (the upgrade and import step)
# ---------------------------------------------------------------------------


def test_add_tokens_maps_the_values_the_tokens_file_declares():
    """The generated file's values, so a starter default a detected value replaced is
    not mapped."""
    source = {
        "index.html": "<html><head></head></html>",
        "a.css": "body { color: #222 } .muted { color: oklch(0.18 0 0) }",
    }
    out, _ = site_tokens.add_tokens(source, None)
    assert out["a.css"] == "body { color: var(--foreground) } .muted { color: oklch(0.18 0 0) }"


def test_add_tokens_with_an_owner_tokens_file_maps_its_root_values():
    tokens = ":root { --foreground: #222; --primary: red; }\n"
    source = {
        "index.html": "<html><head></head></html>",
        "a.css": "body { color: #222222 } a { color: red }",
        site_tokens.TOKENS_REL: tokens,
    }
    out, _ = site_tokens.add_tokens(source, None)
    assert out["a.css"] == "body { color: var(--foreground) } a { color: var(--primary) }"
    assert out[site_tokens.TOKENS_REL] == tokens


def test_a_token_the_tokens_file_overrides_conditionally_is_not_mapped():
    tokens = (
        ":root { --foreground: #222; --background: #fff; }\n"
        "@media (prefers-color-scheme: dark) { :root { --background: #000; } }\n"
    )
    source = {"a.css": "body { color: #222; background: #fff }", site_tokens.TOKENS_REL: tokens}
    out, _ = site_tokens.add_tokens(source, None)
    assert out["a.css"] == "body { color: var(--foreground); background: #fff }"


def test_import_rewrites_the_copied_css():
    from pocketpaw_ee.sites import import_service

    source = {
        "index.html": "<html><head></head><body></body></html>",
        "css/site.css": "body { background: #fdf6ec; color: #3b2f2f }",
        "css/vendor/bootstrap.css": "body { background: #fdf6ec }",
    }
    out = import_service._static_project_source(
        source, ({"paw.template.json": '{"build": "none"}', "AGENTS.md": "x"}, None)
    )
    assert out["css/site.css"] == "body { background: var(--background); color: var(--foreground) }"
    assert out["css/vendor/bootstrap.css"] == source["css/vendor/bootstrap.css"]
