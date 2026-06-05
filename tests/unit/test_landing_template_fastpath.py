# tests/unit/test_landing_template_fastpath.py
# Created: 2026-06-04 (feat/sites-landing-template-fastpath) — proves the
# Paw Site landing fast-path: the create-paw-site brain's STEP-0 keyword
# match routes a landing brief to the ``landing-page`` bundled template
# (so the shared _load_template_block splices a pre-baked skeleton instead
# of cold-drafting the ~260-line conversion tree), and that skeleton uses
# the REAL Ripple marketing widgets by construction with none of the five
# SSR-trap widgets baked in. Three guardrails:
#   1. STEP-0 routing — a landing brief substring-matches the landing-page
#      row in index.json (the exact case-insensitive match the SKILL does).
#   2. Skeleton shape — load_template("landing-page")["ripple_spec"] carries
#      navbar/hero/feature-grid/testimonial/pricing-table/cta/footer and NO
#      form-widget node, NO accordion, NO `plans` key (uses `tiers`).
#   3. Schema — the new ``landing`` pattern validates through PocketTemplate.
"""Tests for the landing-page marketing fast-path template + STEP-0 routing.

The fast-path borrows the proven pocket template mechanism for Paw Sites:
a keyword match loads a hand-authored ``ripple_spec.json`` skeleton and the
shared specialist splice ("INSTANTIATE, DON'T REDESIGN") fills only the
``[bracketed]`` copy. These tests pin both halves — the STEP-0 keyword
routing the SKILL describes, and the skeleton's marketing-widget shape — so
a regression that drops the keywords, swaps in a generic/SSR-trap widget,
or breaks the schema fails loudly instead of silently falling back to the
slow cold-draft path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pocketpaw.bundled_templates.installer import install_bundled_templates
from pocketpaw.bundled_templates.loader import load_template

_BUNDLED_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "pocketpaw" / "bundled_templates" / "_bundled"
)

# The marketing widgets the skeleton MUST use by construction — the whole
# point of the fast-path is that the page is built from the real Ripple
# marketing pack, not hand-rolled grid+card primitives.
_REQUIRED_MARKETING_WIDGETS = {
    "navbar",
    "hero",
    "feature-grid",
    "testimonial",
    "pricing-table",
    "cta",
    "footer",
}

# Widgets that break a statically-rendered (csr=false) Paw Site. The lead
# form must be flat input/textarea/button — never the `form` or
# `newsletter` widget (they nest an invalid <form>); FAQ answers must be
# flat heading/text — never `accordion` (opens only with client JS).
_SSR_TRAP_WIDGETS = {"form", "newsletter", "accordion"}


# ---------------------------------------------------------------------------
# Helpers — the STEP-0 matcher + a rippleSpec tree walker
# ---------------------------------------------------------------------------


def _match_template(brief: str, index: dict[str, Any]) -> str | None:
    """Replicate the create-paw-site SKILL's STEP-0 matcher: lower-case the
    brief and return the slug of the FIRST index row whose any keyword is a
    case-insensitive substring of the brief. Returns None on no match (the
    SKILL then falls back to cold-drafting). This is the routing logic under
    test — kept in lock-step with the SKILL's STEP 0 prose."""
    lowered = brief.lower()
    for row in index.get("templates", []):
        for kw in row.get("keywords", []):
            if kw.lower() in lowered:
                return row["slug"]
    return None


def _iter_nodes(node: Any):
    """Yield every node dict in a rippleSpec tree (depth-first). Handles the
    ``children`` (and, defensively, ``items``) recursion so a trap nested
    deep in a section/card is still reached."""
    if isinstance(node, dict):
        yield node
        for key in ("children", "items"):
            child = node.get(key)
            if isinstance(child, list):
                for c in child:
                    yield from _iter_nodes(c)
            elif isinstance(child, dict):
                yield from _iter_nodes(child)
    elif isinstance(node, list):
        for c in node:
            yield from _iter_nodes(c)


def _widget_types(spec: dict[str, Any]) -> set[str]:
    """Collect every ``type`` string in the spec's ``ui`` tree."""
    ui = spec.get("ui", {})
    return {n["type"] for n in _iter_nodes(ui) if isinstance(n.get("type"), str)}


def _load_index() -> dict[str, Any]:
    return json.loads((_BUNDLED_DIR / "index.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. STEP-0 routing — a landing brief matches the landing-page template
# ---------------------------------------------------------------------------


def test_landing_brief_matches_landing_page_template() -> None:
    """The canonical landing brief substring-matches the landing-page row —
    proving STEP-0 routes it to the fast-path skeleton, not cold-drafting."""
    index = _load_index()
    assert _match_template("a landing site for a dentist", index) == "landing-page"


@pytest.mark.parametrize(
    "brief",
    [
        "build a landing page for my bakery",
        "make a marketing site for my SaaS",
        "I need a sales page for the new course",
        "a website for my plumbing business",
        "can you build a site for our law firm",
        "a marketing page that converts",
    ],
)
def test_varied_landing_briefs_route_to_landing_page(brief: str) -> None:
    """A spread of real-world landing briefs all route to landing-page via
    the registered keywords (landing page / marketing site / sales page /
    website for / site for / marketing page)."""
    assert _match_template(brief, _load_index()) == "landing-page"


def test_non_landing_brief_does_not_match_landing_page() -> None:
    """A dashboard brief must NOT route to landing-page — the keyword set is
    landing-specific, so the matcher returns a different template (or None),
    never the landing skeleton."""
    index = _load_index()
    assert _match_template("a kpi metrics dashboard for revenue", index) != "landing-page"


def test_landing_page_row_has_expected_keywords_and_shape() -> None:
    """The index row carries the registered landing keywords and the
    custom/landing shape+pattern the fast-path relies on."""
    index = _load_index()
    row = next(r for r in index["templates"] if r["slug"] == "landing-page")
    # Row shape matches every sibling row (the chat agent's STEP-0 reader
    # expects exactly these keys).
    assert {"slug", "title", "shape", "pattern", "keywords", "connectors_hint"} <= set(row.keys())
    assert row["shape"] == "custom"
    assert row["pattern"] == "landing"
    for kw in ("landing page", "marketing site", "sales page", "website for", "site for"):
        assert kw in row["keywords"], f"missing STEP-0 keyword {kw!r}"


# ---------------------------------------------------------------------------
# 2. Skeleton shape — real marketing widgets, no SSR traps
# ---------------------------------------------------------------------------


def test_landing_skeleton_loads_with_meta_and_ripple_spec(tmp_path: Path) -> None:
    """``load_template('landing-page')`` returns {meta, ripple_spec} — the
    same contract the shared _load_template_block splices into the prompt."""
    install_bundled_templates(destination_root=tmp_path)
    loaded = load_template("landing-page", templates_dir=tmp_path)
    assert loaded is not None
    assert set(loaded.keys()) == {"meta", "ripple_spec"}
    assert loaded["meta"]["name"] == "landing-page"
    assert loaded["meta"]["pattern"] == "landing"
    spec = loaded["ripple_spec"]
    assert isinstance(spec, dict)
    assert "ui" in spec and "_placeholder_note" in spec


def test_landing_skeleton_uses_the_real_marketing_widgets(tmp_path: Path) -> None:
    """The skeleton is composed from the real Ripple marketing pack BY
    CONSTRUCTION — navbar/hero/feature-grid/testimonial/pricing-table/cta/
    footer all present. This is the assertion that fixes the original
    'uses generic widgets' problem: a regression to grid+card fails here."""
    install_bundled_templates(destination_root=tmp_path)
    spec = load_template("landing-page", templates_dir=tmp_path)["ripple_spec"]
    types = _widget_types(spec)
    missing = _REQUIRED_MARKETING_WIDGETS - types
    assert not missing, f"landing skeleton is missing marketing widgets: {sorted(missing)}"


def test_landing_skeleton_has_no_ssr_trap_widgets(tmp_path: Path) -> None:
    """No form/newsletter/accordion node anywhere in the tree — those break
    a static (csr=false) Paw Site. The lead form is flat inputs instead."""
    install_bundled_templates(destination_root=tmp_path)
    spec = load_template("landing-page", templates_dir=tmp_path)["ripple_spec"]
    types = _widget_types(spec)
    traps = _SSR_TRAP_WIDGETS & types
    assert not traps, f"landing skeleton contains SSR-trap widget(s): {sorted(traps)}"


def test_landing_skeleton_lead_form_is_flat_native(tmp_path: Path) -> None:
    """The lead-capture section is flat input/textarea/button{type:submit}
    with real ``name``s riding the template's outer POST form (SSR rule 1)."""
    install_bundled_templates(destination_root=tmp_path)
    spec = load_template("landing-page", templates_dir=tmp_path)["ripple_spec"]
    nodes = list(_iter_nodes(spec.get("ui", {})))

    inputs = [n for n in nodes if n.get("type") == "input"]
    assert inputs, "lead form must use flat `input` widgets"
    assert all(n.get("props", {}).get("name") for n in inputs), "every input needs a real name="

    submit = [
        n for n in nodes if n.get("type") == "button" and n.get("props", {}).get("type") == "submit"
    ]
    assert submit, "lead form needs a button with type=submit to POST natively"


def test_landing_skeleton_pricing_uses_tiers_not_plans(tmp_path: Path) -> None:
    """pricing-table is populated via ``tiers`` (with a string ``cta`` and a
    symbol ``currency``), never the empty-rendering ``plans``/``columns``
    keys (SSR rule 2)."""
    install_bundled_templates(destination_root=tmp_path)
    spec = load_template("landing-page", templates_dir=tmp_path)["ripple_spec"]
    pricing = next(n for n in _iter_nodes(spec.get("ui", {})) if n.get("type") == "pricing-table")
    props = pricing.get("props", {})
    assert "tiers" in props and isinstance(props["tiers"], list) and props["tiers"]
    assert "plans" not in props, "pricing-table must use `tiers`, not `plans`"
    assert "columns" not in props, "pricing-table must use `tiers`, not `columns`"
    # currency is a symbol, not a code; one tier is popular; tier cta is a string.
    assert props.get("currency") == "$"
    assert any(t.get("popular") for t in props["tiers"]), "mark one tier popular"
    for tier in props["tiers"]:
        assert isinstance(tier.get("cta"), str), "tier cta is a string label, not an object"


def test_landing_skeleton_serialized_spec_carries_marketing_widgets(tmp_path: Path) -> None:
    """Guard the splice path directly: the JSON the shared _load_template_block
    dumps into the prompt (json.dumps(template['ripple_spec'])) contains every
    marketing widget type and none of the SSR traps as a `"type"` token."""
    install_bundled_templates(destination_root=tmp_path)
    spec = load_template("landing-page", templates_dir=tmp_path)["ripple_spec"]
    blob = json.dumps(spec)
    for widget in _REQUIRED_MARKETING_WIDGETS:
        assert f'"{widget}"' in blob, f"spliced spec missing {widget!r}"
    for trap in _SSR_TRAP_WIDGETS:
        assert f'"type": "{trap}"' not in blob, f"spliced spec contains SSR trap {trap!r}"


# ---------------------------------------------------------------------------
# 3. Schema — the new `landing` pattern validates
# ---------------------------------------------------------------------------


def test_landing_template_passes_pydantic_validation() -> None:
    """The landing-page template.pocket.yaml validates through the
    PocketTemplate chokepoint — proving the schema accepts pattern=landing
    with shape=custom (empty columns, no default_view)."""
    import yaml

    from pocketpaw.bundled_templates.schema import PocketTemplate

    meta = yaml.safe_load(
        (_BUNDLED_DIR / "landing-page" / "template.pocket.yaml").read_text(encoding="utf-8")
    )
    template = PocketTemplate.model_validate(meta)
    assert template.name == "landing-page"
    assert template.pattern == "landing"
    assert template.shape == "custom"


def test_landing_pattern_is_in_schema_enum() -> None:
    """``landing`` is a first-class member of PatternT — a defense-in-depth
    check so the enum widening can't silently regress."""
    from typing import get_args

    from pocketpaw.bundled_templates.schema import PatternT

    assert "landing" in get_args(PatternT)


def test_strict_load_validates_landing_template(tmp_path: Path) -> None:
    """strict=True load (the CLI lint / test path) accepts the landing
    template without raising — the schema and the on-disk yaml agree."""
    install_bundled_templates(destination_root=tmp_path)
    loaded = load_template("landing-page", templates_dir=tmp_path, strict=True)
    assert loaded is not None
    assert loaded["meta"]["pattern"] == "landing"


# ---------------------------------------------------------------------------
# 4. Manifest — SSR scaffolding primitives + published marketing widgets
# ---------------------------------------------------------------------------
#
# CAVEAT (the manifest-lag reality, verified 2026-06-04): the runtime
# fetches the PUBLISHED ripple manifest (ripple-iui@latest on the CDN),
# which does NOT yet carry the new marketing category. Of the marketing
# pack only ``hero`` + ``pricing-table`` are in @latest today; ``navbar``,
# ``feature-grid``, ``testimonial``, ``logo-cloud``, ``cta``, ``footer``
# are LIVE on the ripple integration/sites-landing branch but not yet on
# the published CDN. The skeleton intentionally uses the canonical names
# from the shipped landing recipe + the create-paw-site SKILL (the source
# of truth the task points to) — the SAME names the already-merged landing
# recipe, SKILL worked example, and e2e acceptance test use against the
# same published manifest. So this test does NOT hard-fail on the pending
# marketing names (that would block on a CDN-publish lag, not a real bug).
# It pins the parts that ARE verifiable today:
#   - every SSR scaffolding primitive (flex/section/card/input/textarea/
#     button) is manifest-known — these carry the static render + the form
#     POST, so a typo here is a real, catchable break.
#   - the marketing widgets already published (hero, pricing-table) are
#     manifest-known — proving those two names are correct.

_SSR_SCAFFOLD_WIDGETS = {"flex", "section", "card", "input", "textarea", "button"}
# Marketing widgets that are LIVE on the ripple branch but PENDING in the
# published @latest CDN manifest. Asserted present once the CDN catches up.
_MARKETING_PENDING_CDN_PUBLISH = {
    "navbar",
    "feature-grid",
    "testimonial",
    "logo-cloud",
    "cta",
    "footer",
}


def test_landing_skeleton_known_widget_types_are_manifest_valid_if_reachable() -> None:
    """The SSR scaffolding primitives and the already-published marketing
    widgets (hero, pricing-table) are all real manifest widgets — no typo'd
    type renders as the red 'Unknown widget type' box. The marketing widgets
    still pending in the published @latest CDN manifest are exempted (see the
    manifest-lag caveat above) so the test never fails on a CDN-publish lag.
    Skips entirely when the manifest is offline (CI default)."""
    import asyncio

    from pocketpaw.config import get_settings
    from pocketpaw.ripple.manifest import get_manifest

    settings = get_settings()
    manifest = asyncio.run(
        get_manifest(
            settings.ripple_manifest_url,
            ttl_seconds=settings.ripple_manifest_ttl_seconds,
        )
    )
    if manifest is None:
        pytest.skip("ripple manifest not reachable — skipping manifest type check")

    known = {
        w.get("type") for w in (manifest.get("widgets") or []) if isinstance(w.get("type"), str)
    }
    spec = json.loads(
        (_BUNDLED_DIR / "landing-page" / "ripple_spec.json").read_text(encoding="utf-8")
    )
    used = _widget_types(spec)

    # Every scaffolding primitive the skeleton uses must be manifest-known.
    scaffold_unknown = sorted((used & _SSR_SCAFFOLD_WIDGETS) - known)
    assert not scaffold_unknown, f"SSR scaffolding type(s) not in manifest: {scaffold_unknown}"

    # The marketing widgets that the published manifest already carries must
    # validate; the rest are exempted until the CDN publishes the category.
    checkable_marketing = (used & _REQUIRED_MARKETING_WIDGETS) - _MARKETING_PENDING_CDN_PUBLISH
    marketing_unknown = sorted(checkable_marketing - known)
    assert not marketing_unknown, (
        f"published marketing widget(s) not in manifest: {marketing_unknown} "
        "(these are NOT in the pending set, so a miss here is a real name error)"
    )
