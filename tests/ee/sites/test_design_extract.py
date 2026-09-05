# tests/ee/sites/test_design_extract.py — IR-4 (feat/sites-import-design-tokens):
# reading a source site's design language out of its own stylesheets.
#
# Created 2026-09-04. Each test here pins a failure the first live run actually
# produced against rohitk06.in, because every one of them is the kind that makes
# the regenerated page look wrong while the extraction reports success:
#   * "Apple Color Emoji" read as the body typeface, because it is the first
#     non-generic name in a modern font stack;
#   * a 14px heading, because display sizes are rare and fall out of a
#     frequency-ranked scale;
#   * a #121212 page described as "a light palette", because a dark site's TEXT
#     colours are light and outnumber its two grounds;
#   * -100px at the head of the spacing scale, because negative margins are not
#     spacing;
#   * a shadow still naming var(--accent-primary-rgb), which resolves to nothing.
#
# Plus the four things the IR-1 spike measured: var() chains, @font-face as the
# real typeface, shadow tokens outranking colours, and the declaration fallback
# for a site with no custom properties at all.
from __future__ import annotations

from pocketpaw_ee.sites.design_extract import (
    extract_design_system,
    stylesheets_from_crawl,
)

_TOKEN_SITE = """
:root {
  --accent-primary: #f53;
  --bg-primary: #121212;
  --text-primary: #e0e0e0;
  --tw-shadow: 0 1px 3px #0000001a;
  --font-sans: ui-sans-serif, system-ui, sans-serif, "Apple Color Emoji";
  --default-font-family: var(--font-sans);
}
@font-face { font-family: "Roobert Mono"; src: url(/f.woff2); }
h1 { font-size: 8rem; font-weight: 800; color: var(--accent-primary); }
p  { font-size: 1rem; color: var(--text-primary); }
.a { padding: .25rem; margin: -100px; border-radius: 4px; }
.b { padding: .25rem; gap: .5rem; }
.c { background: var(--bg-primary); box-shadow: 0 4px 12px #00000026; }
.d { color: var(--accent-primary); border-color: var(--accent-primary); }
"""


def test_var_chains_resolve_to_literals():
    """--default-font-family is literally var(--font-sans). Unresolved, the brief
    records the string "var(--font-sans)" as a typeface."""
    system, _ = extract_design_system({"a.css": _TOKEN_SITE})
    for face in system.typography.values():
        assert "var(" not in face.family
    assert "var(" not in system.tokens_css


def test_the_real_typeface_comes_from_font_face():
    """--font-sans resolves to a generic system stack; the brand face exists only
    in an @font-face rule."""
    system, _ = extract_design_system({"a.css": _TOKEN_SITE})
    assert system.typography["heading"].family == "Roobert Mono"


def test_emoji_faces_are_not_the_brand_typeface():
    """The first live run read "Apple Color Emoji" as the body font, because it
    is the first non-generic name in that stack."""
    system, _ = extract_design_system({"a.css": _TOKEN_SITE})
    families = {f.family for f in system.typography.values()}
    assert not any("Emoji" in f for f in families), families


def test_a_heading_takes_the_largest_size_the_page_sets():
    """Display sizes are rare by definition, so they fall out of a
    frequency-ranked scale. The first live run reported a 14px heading."""
    system, _ = extract_design_system({"a.css": _TOKEN_SITE})
    assert system.typography["heading"].size == "8rem"


def test_shadow_tokens_do_not_outrank_real_colours():
    """--tw-shadow carries a colour-like value and outranks real colours unless
    excluded by name."""
    system, _ = extract_design_system({"a.css": _TOKEN_SITE})
    assert "tw-shadow" not in system.colors
    assert "accent-primary" in system.colors


def test_the_palette_is_ranked_by_reference_count():
    """A real site declares far more colours than a brief can use, so the order
    IS the selection. The accent is referenced most here."""
    system, _ = extract_design_system({"a.css": _TOKEN_SITE})
    assert next(iter(system.colors)) == "accent-primary"
    assert system.colors["accent-primary"].s500 == "#f53"


def test_a_dark_ground_is_described_as_dark():
    """A dark site's TEXT colours are light and outnumber its grounds, which had
    a #121212 page reported as "a light palette" — inside out."""
    system, _ = extract_design_system({"a.css": _TOKEN_SITE})
    assert "a dark palette" in system.rationale


def test_spacing_carries_no_negatives():
    """Negative margins pull things around; they are not a spacing scale, and
    -100px at the head of one reads as an instruction to overlap."""
    system, _ = extract_design_system({"a.css": _TOKEN_SITE})
    assert system.spacing
    assert not any(v.startswith("-") for v in system.spacing.values())


def test_a_shadow_that_still_names_a_var_is_dropped():
    """It points at a property computed elsewhere, so it resolves to nothing."""
    css = ":root{--x:#fff}\n.a{box-shadow:0 0 0 2px rgba(var(--accent-rgb),.15)}"
    system, _ = extract_design_system({"a.css": css})
    assert all("var(" not in v for v in system.elevation.values())


def test_a_site_with_no_custom_properties_still_yields_a_palette():
    """Custom properties alone covered nothing on the pre-tokens probe site, so
    mining the rules a site emitted is a first-class path, not a degraded one."""
    css = "body{background:#f6f6ef;color:#828282}a{color:#000000}.h{color:#ff6600}"
    system, notes = extract_design_system({"a.css": css})
    assert system.colors
    assert any("no design tokens" in n for n in notes)


def test_near_identical_colours_are_merged():
    """A palette of eight that is really one colour and seven shades of it
    describes nothing. Merging happens in OKLab, where distance tracks what the
    eye sees."""
    css = "\n".join(
        f":root{{--c{i}: #{c}}}" for i, c in enumerate(["ff5533", "ff5534", "ff5532", "121212"])
    )
    css += "\n.a{color:var(--c0)}.b{color:var(--c1)}.c{color:var(--c2)}.d{color:var(--c3)}"
    system, _ = extract_design_system({"a.css": css})
    values = [s.s500 for s in system.colors.values()]
    assert len(values) == 2, values


def test_tokens_css_is_capped_and_declares_what_was_selected():
    system, _ = extract_design_system({"a.css": _TOKEN_SITE})
    assert system.tokens_css.startswith(":root {")
    assert "--accent-primary: #f53;" in system.tokens_css
    assert "--font-heading: Roobert Mono;" in system.tokens_css
    assert len(system.tokens_css) <= 4000


def test_an_empty_stylesheet_set_says_so_rather_than_reporting_a_system():
    system, notes = extract_design_system({})
    assert system.colors == {} and system.typography == {}
    assert any("no stylesheet" in n for n in notes)


def test_inline_style_blocks_are_read():
    """The probe site keeps its whole design system in an inline block; reading
    only linked files would find nothing."""
    html = "<html><head><style>:root{--a:#f53}.x{color:var(--a)}</style></head></html>"
    sheets = stylesheets_from_crawl({"logo.png": b"\x89PNG"}, html)
    assert "<inline>" in sheets
    system, _ = extract_design_system(sheets)
    assert system.colors


# --------------------------------------------------------------------------- #
# The gate: the values have to REACH the model
# --------------------------------------------------------------------------- #


def test_the_preamble_prints_the_token_values():
    """The whole reason a regenerated site looked invented. The design block used
    to say "apply its compiled tokens_css" and "use its palette scales" without
    printing either, so an agent handed a full design system saw no colour, no
    family and no size."""
    from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
    from pocketpaw_ee.cloud.surface.handlers.sites import _frontend_preamble
    from pocketpaw_ee.sites_crew.models import Branding, DesignBrief

    system, _ = extract_design_system({"a.css": _TOKEN_SITE}, name="rohitk06.in")
    brief = DesignBrief(
        goal="Rebuild rohitk06.in", engine="svelte", branding=Branding(design_system=system)
    )
    text = _frontend_preamble(SurfaceMeta(), brief)

    # Assert on VALUES, never on the section rendering: a test that checks the
    # design block is present passes on the version that printed nothing.
    assert "#f53" in text
    assert "#121212" in text
    assert "Roobert Mono" in text
    assert "8rem" in text
    assert "--accent-primary" in text
    assert "```css" in text
