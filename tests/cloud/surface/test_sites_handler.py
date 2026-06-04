# tests/cloud/surface/test_sites_handler.py — Sites surface handler.
#
# Created: 2026-06-03 — Guards the /sites surface preamble.
# Updated: 2026-06-03 (pm) — Bundled skills now load on the SDK backend (local
# plugin via the SDK `plugins=` option), so the preamble PREFERS the
# `pocketpaw-create-site` skill and keeps the raw MCP tools only as a fallback.
# Updated: 2026-06-03 (feat/sites-landing-brain, Task P4) — the preamble now
# prefers the dedicated `pocketpaw-create-paw-site` marketing brain and stamps
# `pattern="landing"`. It also drops the `form-layout` lead-form nudge: the
# `form`/`newsletter` widgets nest an invalid `<form>` inside the static
# template's outer POST form (the broken-dashboard render). The lead-form test
# now pins the FLAT-native-input guidance instead of a form widget.
# Updated: 2026-06-04 (feat/sites-refine-surface) — the /sites surface now has
# TWO modes. The gallery (no pocket_id) still gets the create-a-new-site
# preamble; the per-site refine chat (meta carries a pocket_id) gets a
# LANDING-AWARE REFINE preamble that edits the existing published pocket via
# the merge/edit path instead of rebuilding from scratch. The create-mode tests
# below pin the no-pocket_id branch; the new refine-mode tests pin the
# pocket_id branch (existing-site orientation, edit tool, the same 5 SSR rules,
# and that it does NOT say "build a new site").
# These tests assert the create-mode preamble carries:
#   1. The orientation — surface kind="sites", talk "site" not "pocket".
#   2. The preferred path — the `pocketpaw-create-paw-site` marketing brain.
#   3. The fallback path — the create MCP tool + the publish MCP tool — still
#      present so the flow never breaks when the skill is unavailable.
#   4. A lead-capture form with named fields, built FLAT (no form widget) so the
#      published static site captures leads out of the box.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import sites as sites_handler

pytestmark = pytest.mark.asyncio

WORKSPACE = "ws-surface-sites"
USER = "u-sites"


async def test_sites_handler_carries_orientation() -> None:
    """The preamble still orients: surface=sites, build a site (not a pocket)."""
    preamble = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    assert '<surface kind="sites"' in preamble
    # Talks about the deliverable as a site/page.
    assert "site" in preamble.lower()
    # Must NOT frame the deliverable as a pocket — the agent kept building
    # in-app pockets instead of publishable sites (the reported drift).
    assert "build a pocket" not in preamble.lower()
    assert "build a 'pocket'" not in preamble.lower()


async def test_sites_handler_create_mode_when_no_pocket_id() -> None:
    """The gallery / no-pocket_id case still routes to BUILD AND PUBLISH a NEW
    site — the create branch must not regress now that a refine branch exists."""
    preamble = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    lower = preamble.lower()
    # The create orientation: build a brand-new marketing site.
    assert "build and publish" in lower
    # It must NOT slip into refine framing when there's no pocket to refine.
    assert "refine" not in lower
    assert "existing" not in lower


async def test_sites_handler_prefers_create_paw_site_brain() -> None:
    """The preamble points the agent at the dedicated marketing brain
    (`pocketpaw-create-paw-site`) as the primary path — NOT the generic
    create-pocket flow, which publishes as a broken dashboard."""
    preamble = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    assert "pocketpaw-create-paw-site" in preamble
    # It must be framed as the preferred route, not an afterthought.
    assert "prefer" in preamble.lower()
    # It stamps the landing intent so the generator renders a landing page.
    assert 'pattern="landing"' in preamble


async def test_sites_handler_keeps_mcp_fallback() -> None:
    """The raw MCP tools remain as a fallback so the create→publish flow never
    breaks when the skill is unavailable (e.g. sdk_load_bundled_skills off, or a
    backend without the bundled plugin)."""
    preamble = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    # Step 1 — create the source pocket via the pocket specialist MCP tool.
    assert "mcp__pocketpaw_pocket_specialist__create" in preamble
    # Step 2 — publish it as a live site via the sites manager MCP tool.
    assert "mcp__pocketpaw_sites_manager__publish" in preamble
    # Framed as a fallback, not the primary instruction.
    assert "fall back" in preamble.lower() or "fallback" in preamble.lower()


async def test_sites_handler_specifies_flat_lead_capture_form() -> None:
    """The procedure asks for a FLAT-native lead form with clear field names,
    and explicitly steers OFF the `form`/`newsletter` widget that nests an
    invalid <form> on a static site (the broken-render trap)."""
    preamble = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    lower = preamble.lower()
    # A lead-capture form is part of the marketing/landing build.
    assert "form" in lower
    # At least one concrete, named field so leads are actually captured.
    assert "email" in lower
    # The flat-native rule: it must mention input + submit, and name the
    # form/newsletter widget as the thing to avoid.
    assert "flat" in lower
    assert "newsletter" in lower


# --- Refine mode (meta carries a pocket_id — the per-site /sites/[siteId] chat) ---

REFINE_POCKET = "pkt-existing-site-123"


async def test_sites_handler_refine_mode_orients_to_existing_site() -> None:
    """When the meta carries a pocket_id, the surface is the per-site refine
    chat: the agent must REFINE the EXISTING published site, not create a new
    one and not treat it as a dashboard pocket."""
    preamble = await sites_handler.build_preamble(
        WORKSPACE,
        USER,
        SurfaceMeta(route_path="/sites/site-abc", pocket_id=REFINE_POCKET, site_id="site-abc"),
    )

    lower = preamble.lower()
    # Still the sites surface.
    assert '<surface kind="sites"' in preamble
    # Framed as refining an existing site.
    assert "refine" in lower
    assert "existing" in lower
    # The concrete pocket id is named so the agent edits the right one.
    assert REFINE_POCKET in preamble
    # It must NOT tell the agent to build a new site — that's the regression
    # the per-site chat reported (refine turned into a fresh create).
    assert "build and publish a new" not in lower
    assert "build a new site" not in lower
    # And it must not collapse the site back into a dashboard pocket.
    assert "dashboard pocket" in lower  # names the thing to avoid


async def test_sites_handler_refine_mode_uses_edit_path() -> None:
    """Refine applies the change via the merge/edit path on the existing pocket,
    not the create path — so the published site is updated in place."""
    preamble = await sites_handler.build_preamble(
        WORKSPACE,
        USER,
        SurfaceMeta(route_path="/sites/site-abc", pocket_id=REFINE_POCKET, site_id="site-abc"),
    )

    # The edit/merge tool, not a fresh create.
    assert "mcp__pocketpaw_pocket_specialist__edit" in preamble
    # It should not steer the agent to spin up a brand-new pocket from scratch.
    assert "from scratch" in preamble.lower()  # named as the thing to avoid


async def test_sites_handler_refine_mode_is_landing_aware() -> None:
    """The refine preamble carries the same landing structure + 5 SSR rules as
    the create brain, so an edit can't introduce a static-site trap."""
    preamble = await sites_handler.build_preamble(
        WORKSPACE,
        USER,
        SurfaceMeta(route_path="/sites/site-abc", pocket_id=REFINE_POCKET, site_id="site-abc"),
    )

    lower = preamble.lower()
    # Preserve the landing/conversion structure.
    assert "hero" in lower
    assert "pricing" in lower
    assert "footer" in lower
    # The 5 SSR rules survive an edit:
    # Rule 1 — flat native lead form, never form/newsletter.
    assert "flat" in lower
    assert "newsletter" in lower
    # Rule 2 — pricing-table uses tiers.
    assert "tiers" in lower
    # Rule 3 — FAQ never accordion.
    assert "accordion" in lower
    # Rule 4 — CTAs are anchor href.
    assert "href" in lower
    # Animation Tier-0 only.
    assert "tier-0" in lower or "tier 0" in lower
