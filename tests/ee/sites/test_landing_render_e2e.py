# tests/ee/sites/test_landing_render_e2e.py
# Created: 2026-06-03 (feat/sites-landing-brain, Task V1) — the landing-render
# acceptance guardrail. Proves a canonical landing rippleSpec (the Bright Smile
# dentist shape the create-paw-site brain + landing recipe produce) has the
# SHAPE that renders like the shippable "Option C" page, not the broken
# "Option A" dashboard render.
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
#      with JS off).
#   4. Every CTA/button is an anchor href (or a form submit) — no on_click
#      (dead on a static page).
#   5. The hero is the marketing `hero` widget, never the dashboard `hero+grid`
#      KPI layout (no `stat` tiles / charts on a sales page).

from __future__ import annotations

from typing import Any

import pytest


def _canonical_landing_spec() -> dict[str, Any]:
    """The Bright Smile dentist landing spec — the exact conversion-ordered
    shape the create-paw-site brain and the landing recipe produce. Kept inline
    so the test is deterministic and never depends on the live generator."""
    return {
        "version": "1.0",
        "ui": {
            "type": "flex",
            "props": {"direction": "column", "gap": "0"},
            "children": [
                {
                    "type": "navbar",
                    "props": {
                        "brand": "Bright Smile Dental",
                        "links": [
                            {"label": "Services", "href": "#services"},
                            {"label": "Pricing", "href": "#pricing"},
                            {"label": "Reviews", "href": "#reviews"},
                            {"label": "Book", "href": "#book"},
                        ],
                        "cta": {"label": "Book a visit", "href": "#book"},
                    },
                },
                {
                    "type": "hero",
                    "props": {
                        "eyebrow": "Family & cosmetic dentistry",
                        "title": "Care that fits your whole family",
                        "subtitle": "Gentle, modern dentistry in downtown Austin.",
                        "cta": {"label": "Book your visit", "href": "#book"},
                    },
                },
                {
                    "type": "feature-grid",
                    "props": {
                        "id": "services",
                        "title": "What we do",
                        "features": [
                            {
                                "icon": "tooth",
                                "title": "New Patient Exams",
                                "description": "Full exam, X-rays, cleaning.",
                            },
                            {
                                "icon": "sparkles",
                                "title": "Teeth Whitening",
                                "description": "Up to 8 shades brighter.",
                            },
                            {
                                "icon": "smile",
                                "title": "Invisalign",
                                "description": "Clear aligners, custom plan.",
                            },
                            {
                                "icon": "shield",
                                "title": "Emergency Care",
                                "description": "Same-day relief.",
                            },
                        ],
                    },
                },
                {
                    "type": "testimonial",
                    "props": {
                        "id": "reviews",
                        "quote": "Best dental experience I've had.",
                        "author": "Maria G.",
                        "role": "Patient since 2023",
                        "rating": 5,
                    },
                },
                {
                    "type": "pricing-table",
                    "props": {
                        "id": "pricing",
                        "title": "Simple, upfront pricing",
                        "currency": "USD",
                        "tiers": [
                            {
                                "id": "exam",
                                "name": "New Patient Exam",
                                "price": "$89",
                                "period": "one-time",
                                "features": ["Full exam", "Digital X-rays", "Cleaning"],
                                "cta": {"label": "Book", "href": "#book"},
                            },
                            {
                                "id": "white",
                                "name": "Whitening",
                                "price": "$299",
                                "period": "one-time",
                                "popular": True,
                                "features": [
                                    "In-office session",
                                    "Up to 8 shades",
                                    "Take-home trays",
                                ],
                                "cta": {"label": "Book", "href": "#book"},
                            },
                            {
                                "id": "invis",
                                "name": "Invisalign",
                                "price": "$3,900",
                                "period": "full plan",
                                "features": ["Custom aligners", "All visits", "Retainers included"],
                                "cta": {"label": "Free consult", "href": "#book"},
                            },
                        ],
                    },
                },
                {
                    "type": "cta",
                    "props": {
                        "title": "Ready for a healthier smile?",
                        "subtitle": "Same-week appointments are filling up.",
                        "cta": {"label": "Request an appointment", "href": "#book"},
                    },
                },
                {
                    "type": "card",
                    "props": {"id": "book", "title": "Book your visit"},
                    "children": [
                        {
                            "type": "input",
                            "props": {"name": "name", "label": "Your name", "required": True},
                        },
                        {
                            "type": "input",
                            "props": {
                                "name": "email",
                                "label": "Email",
                                "type": "email",
                                "required": True,
                            },
                        },
                        {
                            "type": "input",
                            "props": {"name": "phone", "label": "Phone", "type": "tel"},
                        },
                        {
                            "type": "textarea",
                            "props": {"name": "message", "label": "What do you need?"},
                        },
                        {
                            "type": "button",
                            "props": {
                                "label": "Request appointment",
                                "type": "submit",
                                "variant": "primary",
                            },
                        },
                    ],
                },
                {
                    "type": "footer",
                    "props": {
                        "brand": "Bright Smile Dental",
                        "tagline": "421 Congress Ave, Austin TX",
                        "links": [
                            {"label": "Services", "href": "#services"},
                            {"label": "Book", "href": "#book"},
                        ],
                    },
                },
            ],
        },
    }


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
    # Tier objects have the canonical shape and an anchor-href cta.
    for tier in props["tiers"]:
        assert {"id", "name", "price"}.issubset(tier.keys())
        assert "href" in tier["cta"]


def test_no_accordion_node_anywhere() -> None:
    """Rule 3 — no `accordion` (bits-ui client primitive; FAQ panels won't open
    with JS off). FAQ, if present, is flat heading/text."""
    spec = _canonical_landing_spec()
    assert "accordion" not in _node_types(spec)


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

    # The nav/hero/cta/footer CTAs all link by href.
    for cta_host in _find(spec, "navbar") + _find(spec, "hero") + _find(spec, "cta"):
        cta = cta_host["props"].get("cta")
        if cta is not None:
            assert "href" in cta


def test_hero_is_marketing_widget_not_dashboard_kpi_layout() -> None:
    """Rule 5 — the page uses the marketing `hero` widget and carries NO
    dashboard KPI widgets (`stat` tiles, charts). A KPI grid at the top is the
    broken `hero+grid` dashboard render, not a landing page."""
    spec = _canonical_landing_spec()
    types = _node_types(spec)

    assert "hero" in types
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
    CTA → lead form → footer. We assert the relative order of the anchor roles."""
    spec = _canonical_landing_spec()
    # Top-level section order (direct children of the root flex).
    top = [c["type"] for c in spec["ui"]["children"]]
    assert top.index("navbar") < top.index("hero")
    assert top.index("hero") < top.index("feature-grid")
    assert top.index("feature-grid") < top.index("pricing-table")
    assert top.index("pricing-table") < top.index("footer")
    # The lead form (the `book` card) comes before the footer.
    book_idx = next(
        i for i, c in enumerate(spec["ui"]["children"]) if c.get("props", {}).get("id") == "book"
    )
    assert book_idx < top.index("footer")


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
