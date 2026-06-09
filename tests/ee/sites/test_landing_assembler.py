# tests/ee/sites/test_landing_assembler.py
# Created: 2026-06-04 (feat/sites-deterministic-fastpath) — KEYSTONE coverage for
# the deterministic Paw Sites fast-path. Proves the marketing widgets survive
# persistence end-to-end, the failure the three prior agent-mode fixes never
# closed.
#
# Updated 2026-06-09 (feat/landing-assembler-enrich, ripple PR #67): the page
# opener is now the bespoke ``marketing-hero`` (was the borrowed dashboard
# ``hero``), and the sample copy carries ``faqs`` so the optional native-<details>
# FAQ section is exercised. Widget-set assertions track ``marketing-hero``; new
# cases pin the marketing-hero CTA split and the FAQ section (present with faqs,
# omitted without).
#
# Two layers:
#   1. Pure assembler (``assemble_landing_spec``) — given an LLM ``content`` copy
#      object, CODE emits the fixed marketing-widget structure. Asserts the
#      widget-type set CONTAINS navbar / marketing-hero / feature-grid /
#      testimonial / pricing-table / cta / footer, a FLAT lead form
#      (input/textarea/button, NO ``form`` widget), section ``id`` anchors + input
#      ``name`` POST fields, the ``tiers`` (not ``plans``) pricing shape, and
#      determinism (same content → identical structure; impossible to downgrade).
#   2. End-to-end create tool (``_create_landing_site_handler``) — against a real
#      (mongomock) Beanie DB it persists the assembled spec via the pockets
#      service ``agent_create`` path and reads the PERSISTED ``_PocketDoc`` back
#      to confirm ``type=="site"`` / ``pattern=="landing"`` and that the
#      marketing widgets actually landed in Mongo (NOT agent narration).
"""Keystone tests for the deterministic landing-site fast-path."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("pocketpaw_ee")


# ---------------------------------------------------------------------------
# A representative LLM-provided ``content`` copy object. COPY ONLY — no
# structure. The assembler decides every widget kind and the node tree.
# Variable-length services / tiers / testimonials exercise the loops.
# ---------------------------------------------------------------------------


def _sample_content() -> dict[str, Any]:
    return {
        "brand": "Bright Smile Dental",
        "hero": {
            "eyebrow": "Family & cosmetic dentistry",
            "title": "Care that fits your whole family",
            "subtitle": "Gentle, modern dentistry in downtown Austin.",
            "cta_label": "Book a visit",
        },
        "services": [
            {
                "title": "New Patient Exams",
                "desc": "Exam, X-rays, cleaning in one visit.",
                "icon": "tooth",
            },
            {
                "title": "Teeth Whitening",
                "desc": "Up to 8 shades brighter in an hour.",
                "icon": "sparkles",
            },
            {"title": "Invisalign", "desc": "Clear aligners with a free consult.", "icon": "smile"},
        ],
        "testimonials": [
            {
                "quote": "Best dental experience I've had.",
                "author": "Maria G.",
                "role": "Patient since 2023",
            },
            {
                "quote": "Booking went from a headache to one tap.",
                "author": "James T.",
                "role": "Patient since 2021",
            },
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
                "features": ["In-office session", "Up to 8 shades", "Take-home trays"],
                "popular": True,
                "cta_label": "Book",
            },
        ],
        "faqs": [
            {
                "question": "How long does a first visit take?",
                "answer": "About 45 minutes — a cleaning, a check, and a plan.",
            },
            {
                "question": "Do you take my insurance?",
                "answer": "We bill all major plans directly.",
            },
        ],
        "cta_band": {
            "headline": "Ready for a healthier smile?",
            "button_label": "Request an appointment",
        },
        "contact": {
            "address": "421 Congress Ave, Austin TX",
            "phone": "(555) 010-1234",
            "email": "hi@brightsmile.com",
        },
        "footer": {"copyright": "© 2026 Bright Smile Dental"},
    }


# ---------------------------------------------------------------------------
# Tree-walk helpers
# ---------------------------------------------------------------------------


def _iter_nodes(node: dict[str, Any]):
    """Depth-first walk over a rippleSpec UI node tree."""
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("children") or []:
        yield from _iter_nodes(child)


def _widget_types(spec: dict[str, Any]) -> set[str]:
    ui = spec.get("ui") or spec
    return {n.get("type") for n in _iter_nodes(ui) if isinstance(n.get("type"), str)}


def _nodes_of_type(spec: dict[str, Any], type_: str) -> list[dict[str, Any]]:
    ui = spec.get("ui") or spec
    return [n for n in _iter_nodes(ui) if n.get("type") == type_]


def _section_ids(spec: dict[str, Any]) -> set[str]:
    ui = spec.get("ui") or spec
    ids: set[str] = set()
    for n in _iter_nodes(ui):
        if n.get("type") in ("section", "card"):
            sid = (n.get("props") or {}).get("id")
            if isinstance(sid, str):
                ids.add(sid)
    return ids


# ---------------------------------------------------------------------------
# 1. The pure assembler — structure is decided by CODE, not the LLM.
# ---------------------------------------------------------------------------


class TestAssembleLandingSpec:
    def test_contains_every_marketing_widget(self) -> None:
        from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

        spec = assemble_landing_spec(_sample_content())
        types = _widget_types(spec)
        for kind in (
            "navbar",
            "marketing-hero",
            "feature-grid",
            "testimonial",
            "pricing-table",
            "faq",
            "cta",
            "footer",
        ):
            assert kind in types, (
                f"marketing widget {kind!r} missing from assembled spec; got {sorted(types)}"
            )
        # The borrowed dashboard hero is gone — the opener is marketing-hero.
        assert "hero" not in types, "use `marketing-hero`, not the borrowed `hero`"

    def test_lead_form_is_flat_never_a_form_widget(self) -> None:
        from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

        spec = assemble_landing_spec(_sample_content())
        types = _widget_types(spec)
        # Flat native inputs ride the site template's outer <form>.
        assert "input" in types
        assert "textarea" in types
        assert "button" in types
        # A nested ``form`` / ``newsletter`` widget would emit its own <form>
        # and silently drop the lead on a static page (SSR rule 1).
        assert "form" not in types, "lead form must be FLAT — no nested `form` widget"
        assert "newsletter" not in types

        # The submit button carries type=submit so the native POST fires.
        buttons = _nodes_of_type(spec, "button")
        assert any((b.get("props") or {}).get("type") == "submit" for b in buttons), (
            "lead form needs a button with type='submit'"
        )

    def test_section_anchors_and_input_names_present(self) -> None:
        from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

        spec = assemble_landing_spec(_sample_content())

        # Anchor targets live on wrapping section/card (marketing widgets carry
        # no id of their own). The navbar links to these.
        anchors = _section_ids(spec)
        for anchor in ("services", "reviews", "pricing", "faq", "book"):
            assert anchor in anchors, (
                f"missing section/card anchor id #{anchor}; got {sorted(anchors)}"
            )

        # Inputs must carry real ``name``s so the native form POST maps fields.
        input_names = {(n.get("props") or {}).get("name") for n in _nodes_of_type(spec, "input")}
        assert {"name", "email"} <= input_names, f"lead inputs need name+email; got {input_names}"
        textarea_names = {
            (n.get("props") or {}).get("name") for n in _nodes_of_type(spec, "textarea")
        }
        assert "message" in textarea_names

    def test_pricing_uses_tiers_not_plans(self) -> None:
        from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

        spec = assemble_landing_spec(_sample_content())
        tables = _nodes_of_type(spec, "pricing-table")
        assert len(tables) == 1
        props = tables[0].get("props") or {}
        assert "tiers" in props, "pricing-table must use `tiers`"
        assert "plans" not in props and "columns" not in props
        assert isinstance(props["tiers"], list) and len(props["tiers"]) == 2
        # currency is a symbol, not a code.
        assert props.get("currency") == "$"
        # one tier marked popular; tier cta is a string label.
        assert any(t.get("popular") for t in props["tiers"])
        for t in props["tiers"]:
            assert isinstance(t.get("cta"), str)

    def test_navbar_cta_uses_href_no_on_click(self) -> None:
        from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

        spec = assemble_landing_spec(_sample_content())
        navbars = _nodes_of_type(spec, "navbar")
        assert len(navbars) == 1
        props = navbars[0].get("props") or {}
        assert props.get("ctaHref") == "#book"
        assert isinstance(props.get("cta"), str)
        # No on_click handlers anywhere — every CTA navigates by anchor.
        ui = spec.get("ui") or spec
        for n in _iter_nodes(ui):
            assert "on_click" not in (n.get("props") or {}), "static site: no on_click CTAs"

    def test_marketing_hero_carries_ctas_and_css_visual(self) -> None:
        """The opener is `marketing-hero` with the hero copy mapped onto it, a
        primary CTA wired to the lead form, a secondary CTA to services, and a
        static-safe CSS `visual`. CTA destinations are sibling href props (never a
        nested object), so they work as plain anchors under csr=false."""
        from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

        spec = assemble_landing_spec(_sample_content())
        heroes = _nodes_of_type(spec, "marketing-hero")
        assert len(heroes) == 1
        p = heroes[0]["props"]
        assert p.get("eyebrow") == "Family & cosmetic dentistry"
        assert p.get("title") and p.get("subtitle")
        # Primary CTA → lead form, as a label + sibling href.
        assert isinstance(p.get("cta"), str) and p["cta"] == "Book a visit"
        assert p.get("ctaHref") == "#book"
        # Secondary ghost CTA → services.
        assert p.get("secondaryCtaHref") == "#services"
        # Premium CSS visual treatment, all static-safe.
        assert p.get("visual") in {"grid", "glow", "plain"}

    def test_faq_section_present_with_faqs_and_omitted_without(self) -> None:
        """The FAQ is OPTIONAL. With `faqs` in the copy the assembler emits a
        `faq` widget (native <details>) under `section#faq`; with no `faqs` the
        section is dropped entirely. Never an `accordion` (JS-only)."""
        from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

        spec = assemble_landing_spec(_sample_content())
        faqs = _nodes_of_type(spec, "faq")
        assert len(faqs) == 1
        items = faqs[0]["props"].get("items")
        assert isinstance(items, list) and len(items) == 2
        for it in items:
            assert it.get("question") and it.get("answer")
        assert "accordion" not in _widget_types(spec)

        # Omitted when the copy has no faqs.
        no_faq = {k: v for k, v in _sample_content().items() if k != "faqs"}
        spec2 = assemble_landing_spec(no_faq)
        assert "faq" not in _widget_types(spec2)
        assert "faq" not in _section_ids(spec2)

    def test_handles_variable_length_collections(self) -> None:
        from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

        content = _sample_content()
        content["services"] = [{"title": "Only one", "desc": "Single service."}]
        content["testimonials"] = [
            {"quote": "Q1", "author": "A"},
            {"quote": "Q2", "author": "B"},
            {"quote": "Q3", "author": "C"},
            {"quote": "Q4", "author": "D"},
        ]
        content["tiers"] = [
            {"name": "Solo", "price": "10", "features": ["x"], "cta_label": "Go"},
        ]
        spec = assemble_landing_spec(content)

        feature_grids = _nodes_of_type(spec, "feature-grid")
        assert len(feature_grids) == 1
        assert len((feature_grids[0]["props"]).get("features", [])) == 1
        # one testimonial node per quote
        assert len(_nodes_of_type(spec, "testimonial")) == 4
        assert len((_nodes_of_type(spec, "pricing-table")[0]["props"])["tiers"]) == 1

    def test_is_deterministic_same_content_identical_structure(self) -> None:
        """The whole point: structure is a pure function of content. Same input
        → byte-identical spec, so the LLM can never downgrade it."""
        from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

        c = _sample_content()
        a = assemble_landing_spec(c)
        b = assemble_landing_spec(c)
        assert a == b, "assembler must be deterministic"

        # And the widget-type SET is invariant regardless of copy values.
        c2 = _sample_content()
        c2["brand"] = "Totally Different Co"
        c2["hero"]["title"] = "Another headline"
        assert _widget_types(a) == _widget_types(assemble_landing_spec(c2))

    def test_copy_lands_in_the_right_widgets(self) -> None:
        from pocketpaw_ee.sites.landing_assembler import assemble_landing_spec

        spec = assemble_landing_spec(_sample_content())
        # brand on navbar
        assert (_nodes_of_type(spec, "navbar")[0]["props"]).get("brand") == "Bright Smile Dental"
        # hero title — on the marketing-hero
        assert (
            (_nodes_of_type(spec, "marketing-hero")[0]["props"]).get("title")
            == "Care that fits your whole family"
        )
        # service titles flow into feature-grid features
        feat_titles = {
            f.get("title") for f in (_nodes_of_type(spec, "feature-grid")[0]["props"])["features"]
        }
        assert "Teeth Whitening" in feat_titles
        # No leftover ``[bracketed]`` placeholders (the skeleton's authoring
        # copy, e.g. "[Business Name]"). Match a '[' immediately followed by a
        # letter — JSON array syntax ('[{', '["') never has that shape.
        import json as _json
        import re as _re

        blob = _json.dumps(spec)
        assert not _re.search(r"\[[A-Za-z]", blob), "no [bracketed] placeholders should remain"


# ---------------------------------------------------------------------------
# 2. End-to-end — the create tool PERSISTS the marketing widgets (ground truth
#    is the Mongo doc, not the agent's narration).
# ---------------------------------------------------------------------------


@pytest.fixture()
def recording_bus():
    """Install a recording EventBus so ``agent_create``'s ``emit(PocketCreated)``
    doesn't raise (the real bus is only wired by ``init_realtime()`` at boot).
    Mirrors the ``tests/cloud/conftest.py`` fixture, which isn't visible from
    this package."""
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod
    from pocketpaw_ee.cloud._core.realtime.events import Event

    class _RecordingBus:
        def __init__(self) -> None:
            self.events: list[Event] = []

        async def publish(self, event: Event) -> None:
            self.events.append(event)

        def subscribe(self, event_type: str, handler) -> None:  # noqa: ARG002
            return

    rec = _RecordingBus()
    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = rec  # type: ignore[attr-defined]
    yield rec
    bus_mod._bus = prev  # type: ignore[attr-defined]


class TestCreateLandingSiteEndToEnd:
    @pytest.mark.asyncio
    async def test_persists_site_with_marketing_widgets(
        self, beanie_test_db, recording_bus
    ) -> None:
        """Drive the tool handler against a real (mongomock) Beanie DB and read
        the persisted ``_PocketDoc`` back. Proves: a pocket lands with
        type=="site" / pattern=="landing", and its rippleSpec CONTAINS the
        marketing widgets — the downgrade the prior fixes never stopped."""
        from bson import ObjectId
        from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp
        from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc

        workspace_id = str(ObjectId())
        user_id = str(ObjectId())

        # Identity flows from the per-stream ContextVars, like the publish tool.
        from unittest.mock import patch

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value=workspace_id,
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value=user_id,
            ),
        ):
            out = await sites_create_mcp._create_landing_site_handler(
                {"content": _sample_content(), "name": "Bright Smile Dental"}
            )

        assert not out.get("is_error"), out
        import json as _json

        body = _json.loads(out["content"][0]["text"])
        assert body["ok"] is True
        pocket_id = body["pocket_id"]
        assert pocket_id

        # Ground truth: read the persisted doc straight from Mongo.
        doc = await _PocketDoc.get(ObjectId(pocket_id))
        assert doc is not None
        assert doc.type == "site"
        assert doc.pattern == "landing"

        persisted_types = _widget_types(doc.rippleSpec)
        for kind in (
            "navbar",
            "marketing-hero",
            "feature-grid",
            "testimonial",
            "pricing-table",
            "faq",
            "cta",
            "footer",
        ):
            assert kind in persisted_types, (
                f"PERSISTED spec dropped marketing widget {kind!r}; got {sorted(persisted_types)}"
            )
        # generic-downgrade guard: the page is NOT a hero+grid+card wireframe, and
        # the borrowed dashboard hero never sneaks back in.
        assert "input" in persisted_types and "form" not in persisted_types
        assert "hero" not in persisted_types

    @pytest.mark.asyncio
    async def test_missing_identity_is_error(self) -> None:
        from unittest.mock import patch

        from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value=None,
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value=None,
            ),
        ):
            out = await sites_create_mcp._create_landing_site_handler(
                {"content": _sample_content(), "name": "X"}
            )
        assert out.get("is_error") is True

    @pytest.mark.asyncio
    async def test_missing_content_is_error(self) -> None:
        from unittest.mock import patch

        from pocketpaw_ee.agent.mcp_servers import sites_create as sites_create_mcp

        with (
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
                return_value="ws_1",
            ),
            patch(
                "pocketpaw_ee.cloud.chat.agent_service.current_user_id",
                return_value="u_1",
            ),
        ):
            out = await sites_create_mcp._create_landing_site_handler({"name": "X"})
        assert out.get("is_error") is True
