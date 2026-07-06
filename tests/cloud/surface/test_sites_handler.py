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
# Updated: 2026-06-04 (feat/sites-svelte-engine) — the CREATE branch now forks on
# `meta.engine` ("ripple" | "svelte"), set by the /sites create UI's "Use Svelte
# pages" toggle. The engine-routing tests at the bottom pin that `engine="svelte"`
# yields a create preamble preferring the `pocketpaw-create-svelte-site` skill
# (and the `create_svelte_site` MCP fallback) and NOT preferring create-paw-site,
# while the default (engine None / "ripple") create preamble is unchanged — still
# names `pocketpaw-create-paw-site`. The toggle does not touch the refine branch.
# These tests assert the create-mode preamble carries:
#   1. The orientation — surface kind="sites", talk "site" not "pocket".
#   2. The preferred path — the `pocketpaw-create-paw-site` marketing brain.
#   3. The fallback path — the create MCP tool + the publish MCP tool — still
#      present so the flow never breaks when the skill is unavailable.
#   4. A lead-capture form with named fields, built FLAT (no form widget) so the
#      published static site captures leads out of the box.
# Updated: 2026-06-05 (feat/sites-svelte-engine, consolidated PR) — aligned the
# svelte-engine create test with the strengthened preamble: the directive moved
# from "prefer" to "this track is MANDATORY ... Use the skill", so the test now
# pins "mandatory" instead of "prefer".
# Updated: 2026-07-06 (feat/sites-crew-create-flow, SC-crew) — the CREATE branch
# now optionally runs a guided two-phase authoring-crew flow behind the
# `settings.sites_crew_enabled` flag (default OFF). The crew tests at the bottom
# pin: (1) flag OFF → the create branch is BYTE-FOR-BYTE `_create_preamble` for
# both ripple and svelte metas, and refine is untouched; (2) flag ON → a create
# meta returns the crew preamble carrying the two-phase structure (interview +
# build), the design-system / stock / palette MCP tool ids, the "just build it"
# escape hatch, and the correct engine skill (`pocketpaw-create-svelte-site` on
# the svelte track, `pocketpaw-create-paw-site` otherwise). The flag is toggled
# via monkeypatch of `pocketpaw.config.get_settings` (not process env).

from __future__ import annotations

from types import SimpleNamespace

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


# --- Engine routing (the /sites create "Use Svelte pages" toggle) ---
#
# The create branch (no pocket_id) forks on meta.engine. engine="svelte" must
# route to the Svelte-track authoring brain; engine None/"ripple" must keep the
# existing ripple marketing brain byte-for-byte.


async def test_create_mode_engine_svelte_prefers_create_svelte_site_skill() -> None:
    """engine="svelte" routes the create preamble to the Svelte-track skill.

    It must MANDATE `pocketpaw-create-svelte-site`, point the MCP fallback at
    `create_svelte_site`, and stamp engine="svelte" — while NOT preferring the
    ripple `create-paw-site` brain."""
    preamble = await sites_handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="svelte")
    )
    lower = preamble.lower()

    # Still the sites surface, now tagged with the svelte engine.
    assert '<surface kind="sites"' in preamble
    assert 'engine="svelte"' in preamble
    # Still a build-AND-publish create flow (not refine).
    assert "build and publish" in lower
    assert "refine" not in lower

    # MANDATORY path: the dedicated Svelte-track authoring skill. The svelte
    # preamble strengthened the directive from "prefer" to "this track is
    # MANDATORY ... Use the skill" — pin the stronger language.
    assert "pocketpaw-create-svelte-site" in preamble
    assert "mandatory" in lower
    # MCP fallback points at the svelte create tool + publish.
    assert "mcp__pocketpaw_sites_manager__create_svelte_site" in preamble
    assert "mcp__pocketpaw_sites_manager__publish" in preamble
    assert "fall back" in lower or "fallback" in lower

    # It must NOT route to the ripple marketing brain on this track. The svelte
    # preamble names `pocketpaw-create-paw-site` only to FORBID it (the strengthened
    # "ABSOLUTELY DO NOT call ... or the pocketpaw-create-paw-site skill" clause).
    assert "pocketpaw-create-paw-site" in preamble
    assert "absolutely do not call" in lower
    # The svelte track explicitly forbids the ripple-spec machinery (the term
    # appears only inside the "no rippleSpec / do not draft one" prohibition).
    assert "no ripplespec" in lower
    assert "do not draft a ripplespec" in lower


async def test_create_mode_default_engine_unchanged_prefers_create_paw_site() -> None:
    """Default engine (None) keeps the ripple create brain: prefers
    `pocketpaw-create-paw-site` and never mentions the svelte track."""
    preamble = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    # Ripple brain preferred, svelte track absent.
    assert "pocketpaw-create-paw-site" in preamble
    assert "create-svelte-site" not in preamble
    assert "create_svelte_site" not in preamble
    assert 'engine="svelte"' not in preamble


async def test_create_mode_engine_ripple_is_byte_identical_to_default() -> None:
    """engine="ripple" is the explicit form of the default — the create preamble
    must be byte-for-byte identical to the no-engine (None) preamble, proving the
    fork only diverges for "svelte"."""
    default_preamble = await sites_handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(route_path="/sites")
    )
    ripple_preamble = await sites_handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="ripple")
    )

    assert ripple_preamble == default_preamble
    # And it is the ripple brain, not the svelte one.
    assert "pocketpaw-create-paw-site" in ripple_preamble
    assert "create-svelte-site" not in ripple_preamble


async def test_engine_threads_through_meta_from_request() -> None:
    """The wire `engine` hint survives DTO→domain mapping so the handler can
    branch on it (mirrors how site_id is threaded)."""
    from pocketpaw_ee.cloud.surface.dto import SurfaceMetaRequest
    from pocketpaw_ee.cloud.surface.service import _meta_from_request

    meta = _meta_from_request(SurfaceMetaRequest(engine="svelte", route_path="/sites"))
    assert meta.engine == "svelte"


# --- Authoring-crew create flow (behind settings.sites_crew_enabled) ---
#
# Default OFF: the create branch returns `_create_preamble(meta)` byte-for-byte.
# Flag ON: a create meta returns the guided two-phase crew preamble. The flag is
# toggled by monkeypatching `pocketpaw.config.get_settings` (the `_crew_enabled`
# helper imports it lazily), never via leaking process env.


def _set_crew_flag(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    """Toggle `sites_crew_enabled` by patching `config.get_settings`."""
    from pocketpaw import config

    fake = SimpleNamespace(sites_crew_enabled=enabled)
    monkeypatch.setattr(config, "get_settings", lambda *a, **k: fake)


async def test_crew_flag_off_is_byte_identical_ripple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag OFF → a ripple create meta returns `_create_preamble` byte-for-byte."""
    _set_crew_flag(monkeypatch, False)
    meta = SurfaceMeta(route_path="/sites")
    out = await sites_handler.build_preamble(WORKSPACE, USER, meta)
    assert out == sites_handler._create_preamble(meta)
    assert 'mode="crew"' not in out


async def test_crew_flag_off_is_byte_identical_svelte(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag OFF → a svelte create meta returns `_create_preamble` byte-for-byte."""
    _set_crew_flag(monkeypatch, False)
    meta = SurfaceMeta(route_path="/sites", engine="svelte")
    out = await sites_handler.build_preamble(WORKSPACE, USER, meta)
    assert out == sites_handler._create_preamble(meta)
    assert 'mode="crew"' not in out


async def test_crew_flag_off_default_settings_still_single_shot() -> None:
    """With no monkeypatch (real settings, flag defaults False) the create branch
    is the shipped single-shot builder — the flag can't regress by default."""
    meta = SurfaceMeta(route_path="/sites")
    out = await sites_handler.build_preamble(WORKSPACE, USER, meta)
    assert out == sites_handler._create_preamble(meta)


async def test_crew_flag_on_returns_two_phase_ripple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag ON → a ripple create meta returns the crew preamble with BOTH phases:
    an interview/questions instruction AND a build instruction."""
    _set_crew_flag(monkeypatch, True)
    out = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    lower = out.lower()

    # Still the sites surface, tagged as the crew flow.
    assert '<surface kind="sites"' in out
    assert 'mode="crew"' in out
    # Two-phase structure: a clarity/interview front AND a build back.
    assert "phase 1" in lower
    assert "phase 2" in lower
    assert "question" in lower  # the interview instruction
    assert "build" in lower


async def test_crew_flag_on_names_design_and_asset_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crew preamble names the design-system, stock, and custom-color tool
    ids so the agent themes the site and wires real assets."""
    _set_crew_flag(monkeypatch, True)
    out = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    assert "mcp__pocketpaw_design_systems__list_design_systems" in out
    assert "mcp__pocketpaw_design_systems__get_design_system" in out
    assert "mcp__pocketpaw_stock__search_stock_images" in out
    # Custom-color path: brand hex → full scale.
    assert "mcp__pocketpaw_palette__scale_from_color" in out


async def test_crew_flag_on_has_just_build_it_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The interview must always offer the 'just build it' out and cap at one
    round of questions so the flow never traps the user."""
    _set_crew_flag(monkeypatch, True)
    out = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    lower = out.lower()

    assert "just build it" in lower
    # One round of questions maximum — the anti-interrogation guard.
    assert "one round" in lower or "never ask more than one" in lower


async def test_crew_flag_on_ripple_uses_ripple_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the ripple (default) engine the crew BUILD step uses the ripple
    marketing skill, not the svelte one."""
    _set_crew_flag(monkeypatch, True)
    out = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))

    assert "pocketpaw-create-paw-site" in out
    assert "create-svelte-site" not in out
    assert 'engine="svelte"' not in out


async def test_crew_flag_on_svelte_uses_svelte_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the svelte engine the crew BUILD step uses the svelte authoring skill
    and stamps engine="svelte" — still a crew preamble with the design tools."""
    _set_crew_flag(monkeypatch, True)
    out = await sites_handler.build_preamble(
        WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="svelte")
    )
    lower = out.lower()

    assert 'mode="crew"' in out
    assert 'engine="svelte"' in out
    assert "pocketpaw-create-svelte-site" in out
    # Same design-system + interview machinery on both engines.
    assert "mcp__pocketpaw_design_systems__list_design_systems" in out
    assert "phase 1" in lower
    # The svelte build step names its create/publish fallback tools.
    assert "mcp__pocketpaw_sites_manager__create_svelte_site" in out


async def test_crew_flag_on_leaves_refine_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag only governs the CREATE branch — a refine meta (pocket_id set)
    still returns the refine preamble, never the crew flow."""
    _set_crew_flag(monkeypatch, True)
    out = await sites_handler.build_preamble(
        WORKSPACE,
        USER,
        SurfaceMeta(route_path="/sites/site-abc", pocket_id=REFINE_POCKET, site_id="site-abc"),
    )

    assert 'mode="refine"' in out
    assert 'mode="crew"' not in out
    assert "phase 1" not in out.lower()
