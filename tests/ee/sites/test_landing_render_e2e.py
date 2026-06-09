# tests/ee/sites/test_landing_render_e2e.py
# Created: 2026-06-03 (feat/sites-landing-brain, Task V1) — the landing-render
# acceptance guardrail. Proves a canonical landing rippleSpec (the Bright Smile
# dentist shape the create-paw-site brain + landing recipe produce) has the
# SHAPE that renders like the shippable "Option C" page, not the broken
# "Option A" dashboard render.
#
# Updated 2026-06-09 (feat/landing-assembler-enrich): the canonical spec is now
# the REAL output of ``assemble_landing_spec`` (the deterministic fast-path) on a
# representative copy object, instead of a hand-maintained dict that drifted from
# the assembler. So these guardrails track exactly what ships. Two new sections
# the enriched assembler emits get their own cases:
#   * the hero is the bespoke ``marketing-hero`` (premium CSS visual, CTAs as
#     ``ctaHref``/``secondaryCtaHref`` siblings — never the borrowed dashboard
#     ``hero``);
#   * an optional ``faq`` section (native <details>, JS-off safe) — present when
#     the copy carries ``faqs``, omitted when it doesn't, and NEVER an
#     ``accordion``.
#
# This is a STATIC, deterministic guardrail over the spec tree — NOT a generator
# render. A full @paw/sites-generator build (install + workerd smoke) is the gold
# proof but is heavy and CI-fragile (Bun, network, a warm ripple tarball), so the
# committed test asserts the structural invariants directly. Each invariant maps
# to one of the five bake-off failures that broke Option A on screen:
#
#   1. Lead form is FLAT input/textarea/button{type:submit} with real name= —
#      NEVER a `form`/`newsletter` widget node (those nest an invalid <form>
#      inside the site template's outer POST form → zero leads captured).
#   2. pricing-table uses `tiers` (not `plans`/`columns`) → populated pricing.
#   3. No `accordion` node anywhere (bits-ui client primitive → FAQ won't open
#      with JS off). FAQ uses the native-<details> `faq` widget instead.
#   4. Every CTA/button is an anchor href (or a form submit) — no on_click
#      (dead on a static page).
#   5. The hero is the marketing `marketing-hero` widget, never the dashboard
#      `hero+grid` KPI layout (no `stat` tiles / charts on a sales page).

from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

# A representative copy object — the Bright Smile dentist brief WITH faqs, so the
# canonical spec exercises every section the assembler can emit (including the
# optional FAQ). The assembler owns the structure; this is COPY only.
_CANONICAL_CONTENT: dict[str, Any] = {
    "brand": "Bright Smile Dental",
    "hero": {
        "eyebrow": "Family & cosmetic dentistry",
        "title": "Care that fits your whole family",
        "subtitle": "Gentle, modern dentistry in downtown Austin.",
        "cta_label": "Book a visit",
    },
    "services": [
        {"title": "New Patient Exams", "desc": "Full exam, X-rays, cleaning.", "icon": "tooth"},
        {"title": "Teeth Whitening", "desc": "Up to 8 shades brighter.", "icon": "sparkles"},
        {"title": "Invisalign", "desc": "Clear aligners, custom plan.", "icon": "smile"},
        {"title": "Emergency Care", "desc": "Same-day relief.", "icon": "shield"},
    ],
    "testimonials": [
        {
            "quote": "Best dental experience I've had.",
            "author": "Maria G.",
            "role": "Patient since 2023",
        },
        {"quote": "Booking was one tap.", "author": "James T.", "role": "Patient since 2021"},
    ],
    "tiers": [
        {
            "name": "New Patient Exam",
            "price": "89",
            "period": "one-time",
            "features": ["Full exam", "Digital X-rays", "Cleaning"],
            "cta_label": "Book",
        },
        {
            "name": "Whitening",
            "price": "299",
            "period": "one-time",
            "popular": True,
            "features": ["In-office session", "Up to 8 shades", "Take-home trays"],
            "cta_label": "Book",
        },
        {
            "name": "Invisalign",
            "price": "3,900",
            "period": "full plan",
            "features": ["Custom aligners", "All visits", "Retainers included"],
            "cta_label": "Free consult",
        },
    ],
    "faqs": [
        {"question": "How long does a visit take?", "answer": "Most first visits run 45 minutes."},
        {"question": "Do you take my insurance?", "answer": "We bill all major plans directly."},
        {"question": "Can I reschedule online?", "answer": "Yes — from the confirmation email."},
    ],
    "cta_band": {
        "headline": "Ready for a healthier smile?",
        "subtext": "Same-week appointments are filling up.",
        "button_label": "Request an appointment",
    },
    "contact": {
        "address": "421 Congress Ave, Austin TX",
        "phone": "(555) 010-1234",
        "email": "hello@brightsmile.com",
    },
    "footer": {"copyright": "© 2026 Bright Smile Dental"},
}


def _canonical_landing_spec() -> dict[str, Any]:
    """The Bright Smile dentist landing spec — the EXACT output of the
    deterministic assembler on ``_CANONICAL_CONTENT``. Driving the real assembler
    (instead of a hand-copied dict) means these guardrails can never silently
    drift from what the create-paw-site fast-path actually ships."""
    return assemble_landing_spec(_CANONICAL_CONTENT)


# Tier-0 (CSS-only, static-safe) animation widgets. Anything else animated needs
# client JS and is forbidden on a static Paw Site.
_TIER0_ANIM = frozenset(
    {"aurora", "marquee", "border-beam", "shimmer", "animated-beam", "text-effect", "bento-grid"}
)
_JS_ONLY_ANIM = frozenset({"reveal", "parallax", "spotlight"})

# Widgets that emit their own nested <form> — invalid inside the template's
# outer POST form, so the lead form must never use them.
_NESTED_FORM_WIDGETS = frozenset({"form", "form-layout", "newsletter"})


def _walk(node: Any):
    """Yield every node dict in a rippleSpec tree (depth-first), descending into
    `children` lists and any dict/list-valued props (so a tier `cta` or a nested
    detail subtree is reached too)."""
    if isinstance(node, dict):
        yield node
        for key, val in node.items():
            if key == "type":
                continue
            yield from _walk(val)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


def _node_types(spec: dict) -> list[str]:
    return [n["type"] for n in _walk(spec.get("ui", spec)) if isinstance(n, dict) and "type" in n]


def _find(spec: dict, type_name: str) -> list[dict]:
    return [
        n for n in _walk(spec.get("ui", spec)) if isinstance(n, dict) and n.get("type") == type_name
    ]


# ---------------------------------------------------------------------------
# The five SSR guardrails (each = a real Option-A failure)
# ---------------------------------------------------------------------------


def test_lead_form_is_flat_native_not_a_form_widget() -> None:
    """Rule 1 — the lead form is flat input/textarea/button{type:submit} with
    real name=, and NO form/newsletter widget node anywhere (those nest an
    invalid <form> inside the template's outer POST form → zero leads)."""
    spec = _canonical_landing_spec()
    types = _node_types(spec)

    # No nested-form-emitting widget anywhere in the tree.
    for bad in _NESTED_FORM_WIDGETS:
        assert bad not in types, f"lead form must be flat; found a `{bad}` widget node"

    # Flat named inputs are present.
    inputs = _find(spec, "input")
    names = {i["props"].get("name") for i in inputs}
    assert "name" in names
    assert "email" in names

    # Exactly one submit button rides the template's outer form.
    submit_buttons = [
        b for b in _find(spec, "button") if b.get("props", {}).get("type") == "submit"
    ]
    assert len(submit_buttons) == 1


def test_pricing_table_uses_tiers_not_plans() -> None:
    """Rule 2 — pricing-table carries `tiers` (the required prop), not
    `plans`/`columns` (which render an empty table)."""
    spec = _canonical_landing_spec()
    pricing = _find(spec, "pricing-table")
    assert len(pricing) == 1
    props = pricing[0]["props"]
    assert "tiers" in props and isinstance(props["tiers"], list) and props["tiers"]
    assert "plans" not in props
    assert "columns" not in props
    assert props["currency"] == "$"
    # Tier objects have the canonical shape and a STRING cta label (the
    # pricing-table renders the button itself; tier cta is never a nested object).
    for tier in props["tiers"]:
        assert {"id", "name", "price"}.issubset(tier.keys())
        assert isinstance(tier["cta"], str) and tier["cta"]


def test_no_accordion_node_anywhere() -> None:
    """Rule 3 — no `accordion` (bits-ui client primitive; FAQ panels won't open
    with JS off). The FAQ section uses the native-<details> `faq` widget, which
    expands with zero client JS, NOT an `accordion`."""
    spec = _canonical_landing_spec()
    assert "accordion" not in _node_types(spec)


# ---------------------------------------------------------------------------
# Enriched-assembler sections (feat/landing-assembler-enrich, ripple PR #67)
# ---------------------------------------------------------------------------


def test_hero_is_marketing_hero_with_premium_css_visual() -> None:
    """The page opener is `marketing-hero` with its premium CSS `visual` set and
    the CTAs wired as `ctaHref`/`secondaryCtaHref` siblings.

    `visual` selects the bespoke CSS panel (dot-grid + glow drift + spec chip),
    which is pure CSS and so paints fully under csr=false. The CTA destinations
    are sibling href props (never a nested object, never on_click)."""
    spec = _canonical_landing_spec()
    heroes = _find(spec, "marketing-hero")
    assert len(heroes) == 1, "exactly one marketing-hero, as the page opener"
    p = heroes[0]["props"]

    # Required headline plus the mapped hero copy.
    assert p.get("title")
    assert p.get("eyebrow") == "Family & cosmetic dentistry"
    assert p.get("subtitle")

    # Premium CSS visual — one of the static-safe panel treatments.
    assert p.get("visual") in {"grid", "glow", "plain"}

    # Primary CTA: string label + sibling href into the lead form.
    assert isinstance(p.get("cta"), str) and p["cta"]
    assert p.get("ctaHref") == "#book"

    # Secondary ghost CTA jumps to services (which always renders).
    assert p.get("secondaryCtaHref") == "#services"


def test_faq_section_is_native_details_when_copy_supplies_faqs() -> None:
    """When the copy carries `faqs`, the assembler emits a `faq` widget (native
    <details>, JS-off safe) wrapped in `section#faq`, carrying the Q/A items —
    and NEVER an `accordion`."""
    spec = _canonical_landing_spec()
    faqs = _find(spec, "faq")
    assert len(faqs) == 1, "one faq widget when faqs are supplied"
    items = faqs[0]["props"].get("items")
    assert isinstance(items, list) and len(items) == 3
    for it in items:
        assert it.get("question") and it.get("answer")
    # It is a native-details `faq`, not the JS-only `accordion`.
    assert "accordion" not in _node_types(spec)
    # The widget is wrapped in an anchored section (marketing widgets carry no id).
    faq_sections = [c for c in spec["ui"]["children"] if c.get("props", {}).get("id") == "faq"]
    assert len(faq_sections) == 1
    assert faq_sections[0]["children"][0]["type"] == "faq"


def test_faq_section_omitted_when_no_faqs_supplied() -> None:
    """FAQ is OPTIONAL: with no `faqs` in the copy, the assembler emits no `faq`
    widget and no `section#faq` — the funnel stays lean. Every other section
    still renders."""
    content = {k: v for k, v in _CANONICAL_CONTENT.items() if k != "faqs"}
    spec = assemble_landing_spec(content)
    assert "faq" not in _node_types(spec)
    assert not any(c.get("props", {}).get("id") == "faq" for c in spec["ui"]["children"])
    # The rest of the funnel is intact.
    assert "marketing-hero" in _node_types(spec)
    assert "pricing-table" in _node_types(spec)


def test_faq_drops_entries_missing_question_or_answer() -> None:
    """A faq entry with no question OR no answer is dropped (an openable-but-blank
    disclosure helps no one); if every entry is unusable the whole section is
    omitted."""
    content = {
        **{k: v for k, v in _CANONICAL_CONTENT.items() if k != "faqs"},
        "faqs": [
            {"question": "Real question?", "answer": "Real answer."},
            {"question": "", "answer": "orphan answer"},  # no question → dropped
            {"question": "orphan question", "answer": ""},  # no answer → dropped
            "not-a-dict",  # not a dict → skipped
        ],
    }
    spec = assemble_landing_spec(content)
    faqs = _find(spec, "faq")
    assert len(faqs) == 1
    items = faqs[0]["props"]["items"]
    assert len(items) == 1 and items[0]["question"] == "Real question?"


def test_all_ctas_are_anchor_hrefs_not_on_click() -> None:
    """Rule 4 — every CTA/button is an anchor href (or the one form submit); no
    on_click handler (dead on a static page)."""
    spec = _canonical_landing_spec()

    # No node in the tree carries an on_click handler.
    for node in _walk(spec["ui"]):
        if isinstance(node, dict):
            assert "on_click" not in node, (
                f"`{node.get('type')}` uses on_click — dead on a static site"
            )
            assert "on_click" not in node.get("props", {})

    # Every button is either a submit or carries an href somewhere in its props.
    for btn in _find(spec, "button"):
        props = btn.get("props", {})
        is_submit = props.get("type") == "submit"
        has_href = "href" in props
        assert is_submit or has_href, "a button must submit or link via href"

    # The navbar + marketing-hero use a STRING `cta` label with the destination in
    # a SEPARATE sibling prop (`ctaHref`), never a nested `{label, href}` object —
    # and never an `on_click`. Assert the label/href split, not a nested href.
    for host in _find(spec, "navbar") + _find(spec, "marketing-hero"):
        p = host["props"]
        assert isinstance(p.get("cta"), str) and p["cta"], "cta is a string label"
        assert p.get("ctaHref", "").startswith("#"), "the destination rides ctaHref"

    # marketing-hero's optional secondary CTA follows the same split.
    for hero in _find(spec, "marketing-hero"):
        p = hero["props"]
        if p.get("secondaryCta"):
            assert p.get("secondaryCtaHref", "").startswith("#")

    # The cta band carries a string `button` label + a sibling `href`.
    for band in _find(spec, "cta"):
        p = band["props"]
        assert isinstance(p.get("button"), str) and p["button"]
        assert p.get("href", "").startswith("#")


def test_hero_is_marketing_widget_not_dashboard_kpi_layout() -> None:
    """Rule 5 — the page uses the bespoke `marketing-hero` widget (NOT the
    borrowed dashboard `hero`) and carries NO dashboard KPI widgets (`stat`
    tiles, charts). A KPI grid at the top is the broken `hero+grid` dashboard
    render, not a landing page."""
    spec = _canonical_landing_spec()
    types = _node_types(spec)

    assert "marketing-hero" in types
    # The borrowed dashboard hero is gone — the page opener is marketing-hero.
    assert "hero" not in types, "use `marketing-hero`, not the borrowed `hero`"
    # No dashboard/analytics widgets on a sales page.
    for dashboardy in ("stat", "chart", "page-header", "pipeline-dashboard", "analytics-dashboard"):
        assert dashboardy not in types, (
            f"`{dashboardy}` is a dashboard widget — not for a landing page"
        )


def test_any_animation_is_tier0_static_safe() -> None:
    """The animation gate — if the spec uses an animated widget it must be
    Tier-0 (CSS-only). A JS-only animator (reveal/parallax/spotlight) hides
    content on a static render."""
    spec = _canonical_landing_spec()
    types = set(_node_types(spec))
    assert not (types & _JS_ONLY_ANIM), "JS-only animation widget on a static site"


def test_conversion_order_is_funnel_top_to_bottom() -> None:
    """The page reads as a funnel: nav → hero → services → proof → pricing →
    FAQ → CTA → lead form → footer. We assert the relative order of the roles.

    Anchored sections wrap their marketing widget in a `section`/`card`, so we
    index by the wrapper's anchor id (services/pricing/faq/book) rather than the
    inner widget type."""
    spec = _canonical_landing_spec()
    top = [c["type"] for c in spec["ui"]["children"]]

    def anchor_idx(anchor: str) -> int:
        return next(
            i
            for i, c in enumerate(spec["ui"]["children"])
            if c.get("props", {}).get("id") == anchor
        )

    # navbar → marketing-hero → services → pricing → footer (top-level types).
    assert top.index("navbar") < top.index("marketing-hero")
    assert top.index("marketing-hero") < anchor_idx("services")
    assert anchor_idx("services") < anchor_idx("pricing")
    assert anchor_idx("pricing") < top.index("footer")
    # FAQ sits between pricing and the closing CTA band (objections answered
    # right before the final ask).
    assert anchor_idx("pricing") < anchor_idx("faq")
    assert anchor_idx("faq") < top.index("cta")
    # The lead form (the `book` card) comes after the CTA band, before the footer.
    assert top.index("cta") < anchor_idx("book")
    assert anchor_idx("book") < top.index("footer")


@pytest.mark.parametrize(
    "trap_spec, why",
    [
        (
            {"ui": {"type": "flex", "children": [{"type": "form", "props": {}}]}},
            "a `form` widget node nests an invalid <form>",
        ),
        (
            {"ui": {"type": "flex", "children": [{"type": "accordion", "props": {}}]}},
            "an `accordion` won't open with JS off",
        ),
    ],
)
def test_guardrail_catches_the_broken_option_a_shapes(trap_spec, why) -> None:
    """Negative control: the guardrail walker actually FINDS the broken shapes
    (so a regression that reintroduces them would fail the asserts above, not
    silently pass). Proves the walker reaches nested nodes."""
    types = _node_types(trap_spec)
    assert ("form" in types) or ("accordion" in types), why
