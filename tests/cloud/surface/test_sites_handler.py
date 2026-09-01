# tests/cloud/surface/test_sites_handler.py — Sites surface handler.
#
# Created: 2026-06-03 — Guards the /sites surface preamble.
# Updated: 2026-09-01 (fix/sites-drop-bundled-design-systems) — the bundled
# DESIGN.md library and its ``pocketpaw_design_systems`` MCP server are gone, so
# ``test_create_names_design_and_asset_tools`` split in two: the asset half kept
# its stock/palette assertions under a truthful name, and the design half became
# an INVERSE guard — no create preamble on any engine may name a design-system
# tool id. A third test pins the replacement instruction, because deleting the
# retriever without saying where tokens come from would leave the model on its
# own defaults, which is the repetition the removal is meant to end.
# Updated: 2026-08-11 (feat/sites-react-edit-lane, RX-3) — four tests at the BOTTOM
# of this file pin the two preambles that route to the new react edit tool: the
# react create step now says a follow-up change is
# `edit_react_component`, NOT a second `create_react_site` (the re-create is what
# minted a SECOND site pocket), and a react refine routes to
# `_react_refine_preamble` instead of the rippleSpec merge path a react pocket has
# nothing to merge into. The fourth test pins that ripple/svelte/no-engine refine is
# byte-for-byte unchanged, so the fork cannot leak into the engine it was not
# written for.
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
# Updated: 2026-07-06 (feat/sites-crew-frontend-brief, SC-2) — added the
# `_frontend_preamble(meta, brief)` tests: it renders the Frontend stage's build
# instructions FROM a structured `DesignBrief` (the crew baton). The new tests pin
# (1) sitemap roles/headings + copy injection from a hand-authored brief, (2)
# engine ROUTING (svelte → create_svelte_site & not the ripple landing tool;
# ripple → create_landing_site & not create_svelte_site), (3) the static-site
# (SSR) rules language survives for a landing brief, and (4) that `build_preamble`
# is UNCHANGED for the live create/refine paths (compared byte-for-byte against
# calling `_create_preamble`/`_refine_preamble` directly), proving the new helper
# is additive and not silently wired into the dispatch.
# Updated: 2026-07-04 (feat/sites-chat-mode, CHAT-BE) — the /sites/[siteId] surface
# now carries a Build/Chat mode. The refine branch (pocket_id present) forks on
# `meta.mode`: "build" (default — today's behavior) keeps the mutate-and-republish
# refine preamble; "chat" routes to a NO-MUTATION Q&A preamble that answers
# questions about the existing site WITHOUT calling pocket_specialist__edit,
# without republishing, and without creating pockets. The new chat-mode tests at
# the bottom pin: (a) chat + pocket_id yields a Q&A preamble that does NOT instruct
# the edit tool / mutation; (b) build (or unset) + pocket_id is byte-identical to
# the existing refine preamble (regression guard); (c) create path (no pocket_id)
# is unaffected by mode.
# Updated: 2026-07-14 (fix/sites-unified-create-preamble) — the three create
# preambles + the `sites_crew_enabled` flag collapsed into ONE always-on
# `_create_preamble`. Every create (no pocket_id) now carries the clarity gate
# (PHASE 1 + `ask_user` chips + "just build it" escape) and the design phase,
# regardless of engine. The build step forks on `meta.engine`: DEFAULT/unset →
# HTML (`create_html_site`); "svelte" → `create_svelte_site`; "ripple" → the
# pocket-specialist landing spec. The old flag tests are gone; the create tests
# below pin the always-asks behavior + per-engine build tool, and the refine/chat
# tests are unchanged.
#
# Updated: 2026-07-17 (feat/sites-draft-first-create) — added
# test_create_is_draft_first_publish_on_request: the create preamble now stops at a
# reviewable DRAFT and offers to publish, gating the publish tool on an EXPLICIT
# go-live request instead of auto-publishing off a plain "create a site". Asserts the
# draft/preview framing + the explicit-go-live gate across all engines, and that the
# publish tool is still named (the on-request path) so an explicit publish is unbroken.
#
# Updated: 2026-08-02 (fix/concierge-tools-for-site-agent) — added the concierge
# awareness block at the bottom. The reported bug ("the agent building sites
# sometimes does not know about concierge at all") was failure mode (2) AWARENESS
# and it was TOTAL: "concierge" appeared zero times in every /sites preamble, zero
# times in the sites MCP tool descriptions, and zero times in the bundled skills,
# so whether the agent mentioned one was decided by model sampling alone. The new
# tests are parametrized across all FIVE dispatched (engine, mode) combinations —
# determinism is the fix, so spot-checking one mode would not prove it — and they
# also pin the block's two honest limits: it must not claim a draft already has a
# concierge (provisioning is live-publish-only) and must not name a configuration
# tool that does not exist (catalog/actions are owner-authored only).

# Updated: 2026-08-18 (fix/sites-html-refine-names-the-edit-tool) — the html
# refine branch was denying a tool that exists. ``edit_html_file`` shipped in
# db083bfc without touching the handler, so the preamble kept telling the agent an
# html site had no edit tool and the agent relayed that to users. Three tests here
# changed: ``test_html_refine_admits_it_has_no_edit_tool`` became
# ``test_html_refine_routes_the_edit_to_the_html_edit_tool`` (it had been
# certifying the stale prompt as correct on every run), and two new ones pin the
# publish step html now earns and the unknown-engine fallback that listed html as
# uneditable. The gate that should have caught this gained its missing direction:
# ``test_every_registered_edit_tool_is_named_by_its_refine_branch`` fails when a
# REGISTERED tool goes unnamed, where the existing gate only failed when a NAMED
# tool was unregistered.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import sites as sites_handler
from pocketpaw_ee.sites_crew.models import (
    AssetRef,
    Branding,
    ColorScale,
    DesignBrief,
    DesignSystem,
    Section,
)

from pocketpaw.sites_capture import contact_form

pytestmark = pytest.mark.asyncio

WORKSPACE = "ws-surface-sites"
USER = "u-sites"


async def test_sites_handler_carries_orientation() -> None:
    """The preamble still orients: surface=sites, build a site (not a pocket)."""
    preamble = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text

    assert '<surface kind="sites"' in preamble
    # Talks about the deliverable as a site/page.
    assert "site" in preamble.lower()
    # Must NOT frame the deliverable as a pocket — the agent kept building
    # in-app pockets instead of publishable sites (the reported drift).
    assert "build a pocket" not in preamble.lower()
    assert "build a 'pocket'" not in preamble.lower()


async def test_sites_handler_create_mode_when_no_pocket_id() -> None:
    """The gallery / no-pocket_id case routes to the unified create flow — always
    the clarity gate first, never refine framing when there's no pocket."""
    preamble = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text

    lower = preamble.lower()
    # The unified create surface, tagged mode="create".
    assert 'mode="create"' in preamble
    # It always opens with the clarity gate, never blind-builds.
    assert "phase 1" in lower
    # It must NOT slip into refine framing when there's no pocket to refine.
    assert 'mode="refine"' not in preamble


async def test_create_default_engine_is_html() -> None:
    """No engine hint → the DEFAULT create engine is html: the build step authors
    a static HTML/CSS bundle via `create_html_site`, NOT the ripple or svelte
    tracks."""
    preamble = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text

    assert 'engine="html"' in preamble
    assert "mcp__pocketpaw_sites_manager__create_html_site" in preamble
    assert "mcp__pocketpaw_sites_manager__publish" in preamble
    # html is MANDATED — create_html_site is the persist tool and the preamble
    # explicitly forbids switching engines for a normal create.
    lower = preamble.lower()
    assert "do not switch engines" in lower
    assert "must use" in lower
    # The svelte/ripple tools appear ONLY after the html one (i.e. in the
    # do-not-use / explicit-exception context, never as the primary path).
    assert preamble.index("create_html_site") < preamble.index("create_svelte_site")


async def test_create_is_draft_first_publish_on_request() -> None:
    """feat/sites-draft-first-create: the create preamble is DRAFT-FIRST. It frames the
    site as a reviewable draft the user previews, tells the agent to STOP at the draft
    and offer to publish, and gates publish on an EXPLICIT go-live request — it no
    longer auto-publishes. The publish tool is still NAMED (the on-request path)."""
    for engine in (None, "svelte", "ripple"):
        preamble = (
            await sites_handler.build_preamble(
                WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine=engine)
            )
        ).text
        lower = preamble.lower()
        # It frames the deliverable as a draft the user previews first.
        assert "draft" in lower
        assert "preview" in lower
        # Publish is conditioned on an explicit go-live ask, not automatic.
        assert "make it live" in lower
        # The old auto-publish framing is gone (the preamble now says "do NOT
        # auto-publish", so guard against the specific old auto-publish claim).
        assert "auto-publishes to a live url" not in lower
        # The publish tool is still available for the on-request path.
        assert "mcp__pocketpaw_sites_manager__publish" in preamble


async def test_create_ripple_engine_uses_pocket_specialist_fallback() -> None:
    """engine="ripple" is the widget-spec track: it prefers the create-paw-site
    marketing brain, stamps `pattern="landing"`, and keeps the pocket-specialist
    MCP tool as the fallback."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="ripple")
        )
    ).text

    assert 'engine="ripple"' in preamble
    assert "pocketpaw-create-paw-site" in preamble
    assert 'pattern="landing"' in preamble
    # Fallback path — the pocket specialist create tool + publish.
    assert "mcp__pocketpaw_pocket_specialist__create" in preamble
    assert "mcp__pocketpaw_sites_manager__publish" in preamble
    assert "fall back" in preamble.lower() or "fallback" in preamble.lower()


async def _preamble_for(engine: str | None) -> str:
    return (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine=engine)
        )
    ).text


async def test_sites_handler_specifies_flat_lead_capture_form() -> None:
    """Every create carries the flat-native lead-form rule so the published static
    site captures leads: fields built FLAT, never a nested form widget."""
    for engine in (None, "ripple", "react"):
        lower = (await _preamble_for(engine)).lower()
        assert "form" in lower
        assert "flat" in lower


async def test_the_tracks_that_author_their_own_form_are_told_where_it_posts() -> None:
    """html and react have no server route, so the form's ACTION is the whole
    difference between a captured lead and a page reload that loses it. This used
    to be missing entirely — the prompt said "a native `<form>` with flat named
    fields" and never said where it posts.

    svelte is in this list too, and it is the one that was quietly worst: the skill
    taught `action="/api/submit"` while `svelte-scaffold.ts` DELETES `src/routes/api`
    for a static site (adapter-static cannot prerender a POST handler), so those
    forms 404'd and the owner read it as nobody filling the form in.

    Asserted per-track rather than over a merged blob: a rule that reaches only one
    of the three authoring tracks is the same silent loss on the others."""
    for engine in (None, "react", "svelte"):  # None == the html default
        preamble = await _preamble_for(engine)
        assert "__CAPTURE_API_BASE__/capture/form" in preamble, f"{engine}: no capture action"
        assert "__CAPTURE_SIGNED_KEY__" in preamble, f"{engine}: no signed key field"
        for field in contact_form.CONTACT_FIELD_NAMES:
            assert field in preamble, f"{engine}: never told to emit {field!r}"


async def test_the_ripple_track_is_not_told_to_author_a_form() -> None:
    """On ripple the form is COMPOSED BY CODE — ``assemble_landing_spec`` builds it
    from ``contact_form.CONTACT_FIELDS`` and the skill supplies copy only. Handing
    that track the native-form contract would tell the agent to write markup it has
    no way to write, and naming the fields would imply it chooses them.

    This is the "the prompt may not command something the agent cannot do" rule
    applied to markup rather than to a tool."""
    preamble = await _preamble_for("ripple")
    assert "__CAPTURE_API_BASE__" not in preamble
    assert "paw_site_id" not in preamble


# --- Refine mode (meta carries a pocket_id — the per-site /sites/[siteId] chat) ---

REFINE_POCKET = "pkt-existing-site-123"


def _refine_meta(pocket_id: str = REFINE_POCKET, **kwargs: str) -> SurfaceMeta:
    """The meta the live refine surface sends: site_id + pocket_id, NO engine.

    Spelled out in one helper because the missing ``engine`` is load-bearing rather
    than incidental — the refine SurfaceMetaProvider in paw-enterprise
    (``routes/sites/[siteId]/+page.svelte``) stamps ``site_id`` / ``pocket_id`` /
    ``focus_node_id`` / ``mode`` and nothing else. A test that passed
    ``engine="react"`` here would be testing a wire shape that does not exist and
    would have reported the engine fork working while every real refine turn got
    the fallback.
    """
    return SurfaceMeta(
        route_path="/sites/site-abc", pocket_id=pocket_id, site_id="site-abc", **kwargs
    )


async def test_sites_handler_refine_mode_orients_to_existing_site() -> None:
    """When the meta carries a pocket_id, the surface is the per-site refine
    chat: the agent must REFINE the EXISTING published site, not create a new
    one and not treat it as a dashboard pocket."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE,
            USER,
            SurfaceMeta(route_path="/sites/site-abc", pocket_id=REFINE_POCKET, site_id="site-abc"),
        )
    ).text

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
    """A RIPPLE refine applies the change via the merge/edit path on the existing
    pocket, not the create path — so the source spec is updated in place.

    Pinned on the ripple branch EXPLICITLY since the engine fork. The specialist
    merges a rippleSpec, and ripple is the only engine whose pocket has one; the
    other three branches must NOT name it (see the engine-fork section below).
    """
    preamble = sites_handler._refine_preamble(_refine_meta(), "ripple")

    # The edit/merge tool, not a fresh create.
    assert "mcp__pocketpaw_pocket_specialist__edit" in preamble
    # It should not steer the agent to spin up a brand-new pocket from scratch.
    assert "from scratch" in preamble.lower()  # named as the thing to avoid


async def test_sites_handler_refine_mode_is_landing_aware() -> None:
    """The RIPPLE refine preamble carries the same landing structure + 5 SSR rules
    as the create brain, so an edit can't introduce a static-site trap.

    These five rules are the ripple branch's and only the ripple branch's: each one
    names a WIDGET type, and a react/html/svelte page is markup with no widgets in
    it (test_source_map_refine_drops_the_ripple_widget_rules).
    """
    preamble = sites_handler._refine_preamble(_refine_meta(), "ripple")

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
    """engine="svelte" routes the build step to the Svelte-track skill.

    It prefers `pocketpaw-create-svelte-site`, points the MCP fallback at
    `create_svelte_site`, stamps engine="svelte", and forbids the ripple-spec
    machinery — while STILL carrying the shared clarity gate."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="svelte")
        )
    ).text
    lower = preamble.lower()

    # Still the sites surface, now tagged with the svelte engine + create mode.
    assert '<surface kind="sites"' in preamble
    assert 'engine="svelte"' in preamble
    assert 'mode="create"' in preamble
    # The shared clarity gate runs on every engine.
    assert "phase 1" in lower

    # The dedicated Svelte-track authoring skill + the merged design-taste helper.
    assert "pocketpaw-create-svelte-site" in preamble
    assert "pocketpaw-design-taste" in preamble
    # MCP fallback points at the svelte create tool + publish.
    assert "mcp__pocketpaw_sites_manager__create_svelte_site" in preamble
    assert "mcp__pocketpaw_sites_manager__publish" in preamble
    assert "unavailable" in lower

    # It must NOT route to the ripple marketing brain or the html tool here.
    assert "pocketpaw-create-paw-site" not in preamble
    assert "create_html_site" not in preamble
    assert "mcp__pocketpaw_sites_manager__create_landing_site" not in preamble
    # The svelte track forbids the ripple-spec machinery.
    assert "no ripplespec" in lower


async def test_create_engine_routing_diverges_by_engine() -> None:
    """The three engines fork ONLY on the build tool — html (default), svelte, and
    ripple each name their own create tool and never each other's."""
    html = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text
    svelte = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="svelte")
        )
    ).text
    ripple = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="ripple")
        )
    ).text

    # Each is a distinct preamble (the fork is real), all in create mode.
    assert html != svelte != ripple != html
    for out in (html, svelte, ripple):
        assert 'mode="create"' in out
        assert "phase 1" in out.lower()

    # html default → the html tool is the PRIMARY/mandated build path (it names
    # the svelte/dynamic tools only later, in the do-not-use / exception context).
    assert "mcp__pocketpaw_sites_manager__create_html_site" in html
    assert html.index("create_html_site") < html.index("create_svelte_site")
    # svelte → the svelte tool only, never the html one.
    assert "mcp__pocketpaw_sites_manager__create_svelte_site" in svelte
    assert "create_html_site" not in svelte
    # ripple → the pocket specialist, not the hand-authored tracks.
    assert "mcp__pocketpaw_pocket_specialist__create" in ripple
    assert "create_html_site" not in ripple
    assert "create_svelte_site" not in ripple


async def test_create_mode_engine_react_prefers_create_react_site_skill() -> None:
    """RX-2: engine="react" routes the build step to the React-track skill.

    It names `pocketpaw-create-react-site`, points at `create_react_site`, stamps
    engine="react", and forbids the ripple-spec machinery — while STILL carrying
    the shared clarity gate."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="react")
        )
    ).text
    lower = preamble.lower()

    assert '<surface kind="sites"' in preamble
    assert 'engine="react"' in preamble
    assert 'mode="create"' in preamble
    assert "phase 1" in lower

    # The dedicated React-track authoring skill + the merged design-taste helper.
    assert "pocketpaw-create-react-site" in preamble
    assert "pocketpaw-design-taste" in preamble
    assert "mcp__pocketpaw_sites_manager__create_react_site" in preamble
    assert "mcp__pocketpaw_sites_manager__publish" in preamble

    # It must NOT route to the other tracks' brains or tools.
    assert "pocketpaw-create-paw-site" not in preamble
    assert "pocketpaw-create-svelte-site" not in preamble
    assert "create_html_site" not in preamble
    assert "create_svelte_site" not in preamble
    assert "mcp__pocketpaw_sites_manager__create_landing_site" not in preamble
    # The react track forbids the ripple-spec machinery.
    assert "no ripplespec" in lower


async def test_react_create_states_the_interactivity_rule() -> None:
    """The react default is the sharp edge of this engine: the site ships ZERO
    JavaScript unless the create declares it, so a page authored with a menu
    toggle or tabs is inert. React is usually chosen BECAUSE of interactivity, so
    the preamble must state the rule — not leave it to the skill, which the model
    may or may not load.

    THE MUTATION THAT BREAKS THIS: delete the `interactive=true` sentence from the
    react `build_step` in handlers/sites.py. Run: every other react assertion
    still passes and this one fails. (Applied 2026-08-07.)"""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="react")
        )
    ).text
    lower = preamble.lower()

    assert "interactive=true" in lower
    # It names what "needs the browser" concretely, so the agent can decide.
    assert "useeffect" in lower or "onclick" in lower
    # And it says what happens when the flag is absent.
    assert "zero javascript" in lower or "no javascript" in lower


async def test_react_create_states_the_prerender_rule() -> None:
    """The prerender rule is engine-shaped, not taste: useEffect does not run at
    prerender time, so a resting state produced only by an effect bakes the START
    frame into the deployed HTML. The svelte track carries the same rule for the
    same reason."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="react")
        )
    ).text
    lower = preamble.lower()

    assert "prerender" in lower
    assert "useeffect" in lower
    assert "markup" in lower


async def test_react_create_names_the_reserved_build_shell() -> None:
    """The generator owns index.html / package.json / vite.config.ts /
    paw-prerender.mjs and the src/paw/ namespace, and REJECTS a source map that
    writes one. A preamble that doesn't say so sends the agent into a create error
    it cannot predict — and those files are what hold the prerender contract."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="react")
        )
    ).text

    assert "src/App.tsx" in preamble
    for reserved in ("index.html", "package.json", "vite.config.ts", "paw-prerender.mjs"):
        assert reserved in preamble, f"the preamble does not name the reserved {reserved}"
    assert "src/paw/" in preamble


async def test_react_create_does_not_promise_a_submit_route() -> None:
    """There is no server runtime on the react track, so the SvelteKit skeleton's
    `/api/submit` endpoint does not exist here. Promising it would be the prompt
    offering a capability the deployment lacks — the same class as naming an
    absent tool — and the agent would wire a form that silently drops leads.

    The preamble must say so EXPLICITLY rather than merely stay silent: the
    authoring skill and the svelte track both teach `/api/submit`, so an agent
    carrying that habit needs it revoked, not omitted."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="react")
        )
    ).text
    lower = preamble.lower()

    assert "no `/api/submit` route" in lower
    # And it names the alternative, so revoking the habit leaves somewhere to go.
    assert "native `<form>`" in lower


async def test_react_create_uses_ask_user_tool_not_ripple() -> None:
    """react-create has inline ripple OFF (surface_registry._sites_profile), so the
    ask mechanism must be the `ask_user` MCP tool, NOT a ```ui-spec ripple widget.

    Telling a ripple-off agent to emit a ui-spec block is not an error — it
    renders as a fenced block of raw JSON the user is expected to read. That is
    the silent-improvisation failure mode, so the preamble and the profile must
    agree. Guarded from the profile side by
    test_sites_react_create_drops_ripple_and_denies."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="react")
        )
    ).text

    assert "mcp__pocketpaw_ask__ask_user" in preamble
    assert "ask-user-questions" not in preamble


async def test_unknown_engine_still_falls_back_to_html() -> None:
    """An engine string this build predates (or a typo) must not render a
    half-formed preamble — it falls back to the html default, the same policy
    sites/engines.py::normalize_engine applies on the publish side."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="solid")
        )
    ).text

    assert 'engine="html"' in preamble
    assert "mcp__pocketpaw_sites_manager__create_html_site" in preamble


async def test_html_default_routes_an_explicit_react_ask_to_the_react_tool() -> None:
    """The html preamble's escape hatch previously sent 'explicitly asks for a
    React/component build' to `create_svelte_site` — correct only while no react
    engine existed. Now that it does, an explicit React request must reach the
    react tool, or the user asks for React and gets Svelte."""
    preamble = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text

    assert "mcp__pocketpaw_sites_manager__create_react_site" in preamble
    # The react tool is named in the EXCEPTION context, after the mandated html
    # one — html is still the default engine.
    assert preamble.index("create_html_site") < preamble.index("create_react_site")


async def test_engine_threads_through_meta_from_request() -> None:
    """The wire `engine` hint survives DTO→domain mapping so the handler can
    branch on it (mirrors how site_id is threaded)."""
    from pocketpaw_ee.cloud.surface.dto import SurfaceMetaRequest
    from pocketpaw_ee.cloud.surface.service import _meta_from_request

    meta = _meta_from_request(SurfaceMetaRequest(engine="svelte", route_path="/sites"))
    assert meta.engine == "svelte"


# --- Unified always-on create flow (the guided two-phase authoring gate) ---
#
# There is no `sites_crew_enabled` flag anymore: EVERY create (no pocket_id) runs
# the clarity gate + design phase. These tests pin the always-on behavior, the
# design/asset tool ids, the "just build it" escape hatch, and that the flow never
# leaks into the refine branch.


async def test_create_always_two_phase() -> None:
    """Every create meta returns the two-phase flow: an interview/clarity front
    (Phase 1) AND a design+build back (Phase 2) — no flag required."""
    out = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text
    lower = out.lower()

    assert '<surface kind="sites"' in out
    assert 'mode="create"' in out
    # Two-phase structure: a clarity/interview front AND a build back.
    assert "phase 1" in lower
    assert "phase 2" in lower
    assert "question" in lower  # the interview instruction
    assert "build" in lower


async def test_create_names_asset_tools() -> None:
    """The create preamble names the stock and custom-color tool ids so the agent
    wires real assets and honours a brand hex."""
    out = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text

    assert "mcp__pocketpaw_stock__search_stock_images" in out
    # Custom-color path: brand hex → full scale.
    assert "mcp__pocketpaw_palette__scale_from_color" in out


async def test_create_never_names_a_bundled_design_system_tool() -> None:
    """The preamble must not send the agent shopping for a canned look.

    The bundled DESIGN.md library and its ``pocketpaw_design_systems`` MCP server
    are DELETED (fix/sites-drop-bundled-design-systems). Five fixed identities
    capped how many different sites this surface could produce, and the taxonomy
    made the pick near-deterministic — ``warm-local-service`` claimed cafe /
    salon / dentist / bakery, so every local business got the same honey-amber
    palette, the one the embedded design system bans as a default reach.

    This is the INVERSE guard, and it is the one that matters: an id named here
    is an id the agent tries to call. Re-adding the library would have to re-add
    this name, and that is what should fail.

    MUTATION: put ``mcp__pocketpaw_design_systems__list_design_systems`` back into
    the create preamble's Phase 2 and this test fails.
    """
    for engine in (None, "html", "ripple", "svelte", "react"):
        out = (
            await sites_handler.build_preamble(
                WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine=engine)
            )
        ).text
        assert "pocketpaw_design_systems" not in out, f"engine={engine}"
        assert "list_design_systems" not in out, f"engine={engine}"
        assert "get_design_system" not in out, f"engine={engine}"


async def test_create_tells_the_agent_to_author_its_own_tokens() -> None:
    """With no library to retrieve, Phase 2 must say where the tokens come from.

    Deleting the retriever without replacing the instruction would leave step 1
    saying nothing about color, type, or ground — and an absence of instruction
    is how the model falls back to its own defaults, which is the repetition this
    change exists to remove. So the step names the custom properties to write and
    points at the embedded design system's own modules for the values.
    """
    out = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text

    assert "--accent" in out
    assert "--bg" in out
    assert "custom properties" in out.lower()
    # The anti-repetition rule is now load-bearing rather than a footnote.
    assert "rotate" in out.lower()
    assert "reseed the accent" in out


async def test_create_clarify_renders_ripple_widget_when_ripple_on() -> None:
    """On ripple-ON create surfaces (html default, ripple) the clarity question is
    rendered as a COMPLETE UI: an `ask-user-questions` ripple widget (ui-spec
    block) whose completeActions emit chat.send — NOT the ask_user chip tool."""
    for engine in (None, "html", "ripple"):
        out = (
            await sites_handler.build_preamble(
                WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine=engine)
            )
        ).text
        assert "ask-user-questions" in out
        assert "ui-spec" in out
        assert '"target": "chat.send"' in out
        # The ripple-widget path does NOT reach for the chip tool.
        assert "mcp__pocketpaw_ask__ask_user" not in out


async def test_create_clarify_uses_ask_user_tool_on_svelte() -> None:
    """The svelte track has inline ripple OFF, so the clarity question falls back
    to the `ask_user` MCP chip tool (the agent can't render a ripple widget
    there)."""
    out = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="svelte")
        )
    ).text
    assert "mcp__pocketpaw_ask__ask_user" in out
    assert "ask-user-questions" not in out


async def test_create_embeds_design_system_inline() -> None:
    """PERMANENT FIX: the full pocketpaw-design-taste system is EMBEDDED in the
    create preamble (not left to a model-driven skill invocation the agent may
    skip). A regression that drops the embed would silently return sites to
    generic AI output."""
    out = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text
    # The governing block + load-bearing body markers from the skill file.
    assert '<design-system name="pocketpaw-design-taste">' in out
    assert "</design-system>" in out
    assert "Vision Ledger" in out
    assert "Trend Engine" in out
    assert "PRE-FLIGHT" in out.upper()
    # It is framed as already-loaded so the agent doesn't wait on a skill call.
    assert "ALREADY LOADED" in out or "already in your context" in out.lower()


async def test_create_decides_design_and_uses_taste_skill() -> None:
    """The create preamble tells the agent to DECIDE the look itself (infer via
    the pocketpaw-design-taste skill), NEVER ask the user for the theme, and
    never fabricate real-world facts."""
    out = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text
    lower = out.lower()
    # Design is inferred via the taste skill, applied throughout the build.
    assert "pocketpaw-design-taste" in out
    assert "decide the design yourself" in lower
    # Explicitly forbids asking the user what visual style / theme to use.
    assert "never ask" in lower
    assert "theme" in lower
    # Facts are never fabricated.
    assert "fabricate" in lower


async def test_refine_states_ask_dont_assume_principle() -> None:
    """The refine preamble also tells the agent to ask (not guess) on ambiguous
    edits and never fabricate facts."""
    out = (
        await sites_handler.build_preamble(
            WORKSPACE,
            USER,
            SurfaceMeta(route_path="/sites/site-abc", pocket_id=REFINE_POCKET, site_id="site-abc"),
        )
    ).text
    lower = out.lower()
    assert "ask, don't assume" in lower
    assert "fabricate" in lower


async def test_create_has_just_build_it_escape_hatch() -> None:
    """The interview must always offer the 'just build it' out and cap at one
    round of questions so the flow never traps the user."""
    out = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text
    lower = out.lower()

    assert "just build it" in lower
    # One round of questions maximum — the anti-interrogation guard.
    assert "one round" in lower or "never ask more than one" in lower


async def test_create_leaves_refine_untouched() -> None:
    """The unified create flow only governs the CREATE branch — a refine meta
    (pocket_id set) still returns the refine preamble, never the create flow."""
    out = (
        await sites_handler.build_preamble(
            WORKSPACE,
            USER,
            SurfaceMeta(route_path="/sites/site-abc", pocket_id=REFINE_POCKET, site_id="site-abc"),
        )
    ).text

    assert 'mode="refine"' in out
    assert 'mode="create"' not in out
    assert "phase 1" not in out.lower()


# --- Frontend-consumes-brief (`_frontend_preamble`) — the crew baton, end to end ---
#
# `_frontend_preamble(meta, brief)` renders the Frontend stage's build
# instructions FROM a structured DesignBrief instead of a raw user message. These
# tests prove the baton flows in and correct, engine-routed build instructions
# flow out. `_frontend_preamble` is SYNC; the tests call it directly inside async
# bodies (the module-level asyncio marker applies to every test here).


def _landing_brief(engine: str = "ripple") -> DesignBrief:
    """A hand-authored DesignBrief fixture for a dentist landing page."""
    return DesignBrief(
        goal="Book more new-patient appointments for a family dental clinic",
        audience="Local families searching for a nearby dentist",
        engine=engine,  # type: ignore[arg-type]
        pattern="landing",
        sitemap=[
            Section(id="nav", role="nav", heading="BrightSmile Dental"),
            Section(id="hero", role="hero", heading="A Brighter Smile Starts Here"),
            Section(id="services", role="services", heading="Our Services"),
            Section(id="pricing", role="pricing", heading="Simple, Honest Pricing"),
            Section(id="lead", role="lead_form", heading="Book Your Visit"),
            Section(id="footer", role="footer"),
        ],
        branding=Branding(
            design_system=DesignSystem(
                name="BrightSmile",
                colors={"primary": ColorScale(s500="#0ea5e9")},  # type: ignore[call-arg]
                rationale="Calm, clinical, trustworthy — avoid loud reds and clutter.",
                tokens_css=":root{--color-primary:#0ea5e9;}",
            ),
            voice="Warm, reassuring, plain-spoken — no dental jargon.",
        ),
        copy={
            "hero": {
                "headline": "A Brighter Smile Starts Here",
                "subhead": "Gentle, modern dentistry for the whole family.",
            },
            "services": {"body": "Cleanings, whitening, implants, and emergency care."},
        },
        asset_manifest=[
            AssetRef(
                url="https://images.pexels.com/photos/1/dentist.jpg",
                kind="image",
                alt="Smiling dentist with a patient",
            ),
        ],
    )


async def test_frontend_preamble_renders_sitemap_and_copy_from_brief() -> None:
    """Given a hand-authored brief, the preamble is non-empty and carries the
    section roles/headings from the sitemap AND the copy from `brief.copy`."""
    brief = _landing_brief()
    preamble = sites_handler._frontend_preamble(SurfaceMeta(route_path="/sites"), brief)

    assert isinstance(preamble, str) and preamble.strip()
    # Still the sites surface, framed as a site not a pocket.
    assert '<surface kind="sites"' in preamble
    assert "build a pocket" not in preamble.lower()

    # Section roles + headings from the ordered sitemap are present.
    assert "hero" in preamble
    assert "services" in preamble
    assert "A Brighter Smile Starts Here" in preamble
    assert "Our Services" in preamble

    # The copy blocks keyed by section id are injected verbatim.
    assert "Gentle, modern dentistry for the whole family." in preamble
    assert "Cleanings, whitening, implants, and emergency care." in preamble

    # Sections are ordered: nav before hero before footer.
    assert preamble.index("A Brighter Smile Starts Here") < preamble.index("Book Your Visit")

    # The real asset URL from the manifest is used, not a placeholder.
    assert "https://images.pexels.com/photos/1/dentist.jpg" in preamble

    # Design system tokens + voice are threaded through.
    assert "BrightSmile" in preamble
    assert "tokens_css" in preamble
    assert "avoid loud reds and clutter" in preamble
    assert "no dental jargon" in preamble


async def test_frontend_preamble_routes_svelte_to_create_svelte_site() -> None:
    """engine="svelte" routes the build to `create_svelte_site` and must NOT
    name the ripple landing tool."""
    brief = _landing_brief(engine="svelte")
    preamble = sites_handler._frontend_preamble(SurfaceMeta(route_path="/sites"), brief)

    assert 'engine="svelte"' in preamble
    assert "create_svelte_site" in preamble
    # The ripple landing tool must be absent on the svelte track.
    assert "create_landing_site" not in preamble


async def test_frontend_preamble_routes_ripple_to_landing_tool() -> None:
    """engine="ripple" routes the build to the ripple create path
    (`create_landing_site`) and must NOT name `create_svelte_site`."""
    brief = _landing_brief(engine="ripple")
    preamble = sites_handler._frontend_preamble(SurfaceMeta(route_path="/sites"), brief)

    assert 'engine="ripple"' in preamble
    assert "create_landing_site" in preamble
    # The svelte create tool must be absent on the ripple track.
    assert "create_svelte_site" not in preamble


async def test_frontend_preamble_preserves_ssr_rules_for_landing_brief() -> None:
    """The static-site (SSR) rules language survives into the brief-driven
    preamble — flat lead form (not form/newsletter), pricing-table tiers, and
    anchor CTAs — so a brief-driven build can't reintroduce a static-site trap."""
    brief = _landing_brief()
    preamble = sites_handler._frontend_preamble(SurfaceMeta(route_path="/sites"), brief)
    lower = preamble.lower()

    # Rule 1 — flat native lead form, never the form/newsletter widget.
    assert "flat" in lower
    assert "newsletter" in lower
    # Rule 2 — pricing-table uses tiers.
    assert "tiers" in lower
    # Rule 3 — FAQ never accordion.
    assert "accordion" in lower
    # Rule 4 — CTAs are anchor href, never on_click.
    assert "href" in lower
    assert "on_click" in lower
    # Animation Tier-0 only.
    assert "tier-0" in lower or "tier 0" in lower


async def test_build_preamble_unchanged_for_create_path() -> None:
    """`_frontend_preamble` is ADDITIVE — the live create dispatch is unchanged.
    A create meta (no pocket_id) still returns exactly `_create_preamble`'s
    output, proving the new helper is not silently wired into the flow."""
    meta = SurfaceMeta(route_path="/sites")
    live = (await sites_handler.build_preamble(WORKSPACE, USER, meta)).text

    assert live == sites_handler._create_preamble(meta)
    # And it is NOT the brief-driven preamble.
    assert 'mode="frontend"' not in live


async def test_build_preamble_unchanged_for_refine_path() -> None:
    """A refine meta (with pocket_id) still returns exactly `_refine_preamble`'s
    output — the additive brief helper does not touch the refine dispatch.

    With no pocket seeded the engine lookup finds nothing, so the live dispatch
    renders the unknown-engine branch; comparing against ``_refine_preamble(meta,
    None)`` states that outright instead of relying on the default argument
    happening to match.
    """
    meta = _refine_meta()
    live = (await sites_handler.build_preamble(WORKSPACE, USER, meta)).text

    assert live == sites_handler._refine_preamble(meta, None)
    assert 'mode="frontend"' not in live


# --- Chat mode (the /sites/[siteId] Build/Chat toggle set to Chat) ---
#
# The refine branch (pocket_id present) forks on meta.mode. mode="chat" must
# route to a NO-MUTATION Q&A preamble; mode="build" (or unset) keeps today's
# mutate-and-republish refine preamble byte-for-byte.

CHAT_POCKET = "pkt-existing-site-chat-456"


async def test_sites_handler_chat_mode_is_no_mutation_qa() -> None:
    """mode="chat" + pocket_id yields a Q&A preamble that ANSWERS questions about
    the existing site WITHOUT editing it: it must NOT instruct the edit tool, must
    NOT republish, and must NOT create a new pocket."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE,
            USER,
            SurfaceMeta(
                route_path="/sites/site-abc",
                pocket_id=CHAT_POCKET,
                site_id="site-abc",
                mode="chat",
            ),
        )
    ).text

    lower = preamble.lower()
    # Still the sites surface, tagged as chat mode.
    assert '<surface kind="sites"' in preamble
    assert 'mode="chat"' in preamble
    # Oriented to the EXISTING site the user is chatting about.
    assert "existing" in lower
    assert CHAT_POCKET in preamble
    # It must READ as a question/answer surface.
    assert "question" in lower or "answer" in lower
    # NO-MUTATION: never the edit tool, never a republish, never a new pocket.
    assert "mcp__pocketpaw_pocket_specialist__edit" not in preamble
    assert "republish" not in lower
    assert "do not modify" in lower or "do not edit" in lower or "without editing" in lower
    # Keeps the site/page vocabulary (not "pocket" as the deliverable).
    assert "site" in lower


async def test_sites_handler_build_mode_identical_to_refine() -> None:
    """mode="build" (and unset) + pocket_id is byte-for-byte the existing refine
    preamble — the Build side of the toggle is today's behavior, unchanged."""
    unset = (
        await sites_handler.build_preamble(
            WORKSPACE,
            USER,
            SurfaceMeta(route_path="/sites/site-abc", pocket_id=REFINE_POCKET, site_id="site-abc"),
        )
    ).text
    build = (
        await sites_handler.build_preamble(
            WORKSPACE,
            USER,
            SurfaceMeta(
                route_path="/sites/site-abc",
                pocket_id=REFINE_POCKET,
                site_id="site-abc",
                mode="build",
            ),
        )
    ).text

    # The toggle's Build side is exactly the refine preamble; unset defaults to it.
    assert build == unset
    # And it is the refine preamble, not the chat one (regression guard). The edit
    # TOOL it names is engine-dependent since the fork, so the mode tag is what
    # this test can assert without re-pinning ripple's tool on every engine.
    assert 'mode="refine"' in build
    assert 'mode="chat"' not in build


async def test_sites_handler_chat_mode_ignored_without_pocket_id() -> None:
    """mode="chat" with NO pocket_id is still the create surface — chat mode only
    applies to an existing site, so the gallery/create path is unaffected."""
    chat_no_pocket = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", mode="chat")
        )
    ).text
    plain_create = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text

    # No pocket to chat about → the create preamble is unchanged by mode.
    assert chat_no_pocket == plain_create
    assert 'mode="create"' in chat_no_pocket
    assert "phase 1" in chat_no_pocket.lower()


async def test_mode_threads_through_meta_from_request() -> None:
    """The wire `mode` hint survives DTO→domain mapping so the handler can branch
    on it (mirrors how engine/site_id are threaded). Default is "build"."""
    from pocketpaw_ee.cloud.surface.dto import SurfaceMetaRequest
    from pocketpaw_ee.cloud.surface.service import _meta_from_request

    meta = _meta_from_request(SurfaceMetaRequest(mode="chat", route_path="/sites"))
    assert meta.mode == "chat"
    # Default preserves current (build) behavior when the client omits it.
    default_meta = _meta_from_request(SurfaceMetaRequest(route_path="/sites"))
    assert default_meta.mode == "build"


# --- Concierge awareness (fix/concierge-tools-for-site-agent) -----------------
#
# THE BUG: an agent building a site "sometimes does not know about concierge at
# all". Diagnosis was failure mode (2) AWARENESS, and it was total: before this
# fix the string "concierge" appeared ZERO times in every /sites preamble
# (create/refine/chat/frontend), ZERO times in the sites MCP tool descriptions
# (`agent/mcp_servers/sites.py`, `sites_create.py`), and ZERO times anywhere in
# `src/pocketpaw/bundled_skills/`. Nothing in the code path ever stated a
# concierge exists, so whether the agent mentioned one was decided purely by
# model sampling — which is exactly what "sometimes" looks like from outside.
#
# Updated: 2026-08-11 (fix/sites-refine-preamble-engine-fork) — the REFINE branch
# forks by engine, so its tests do too. Three things changed shape here:
#   1. The engine comes from the POCKET, not from `meta`, so the tests that prove
#      the fork seed a real site pocket and go through `build_preamble`. A test that
#      passed `engine=` into `_refine_preamble` would have passed against an
#      implementation that reads `meta.engine` — which the live surface never sends,
#      so every real refine turn would have taken the fallback. `_refine_meta()`
#      exists to keep that wire shape (site_id + pocket_id + mode, no engine) in one
#      place.
#   2. `test_sites_handler_refine_mode_uses_edit_path` and
#      `..._is_landing_aware` are now pinned to RIPPLE explicitly. They were
#      written when there was one branch; the tool and the five widget rules they
#      assert belong to that engine alone.
#   3. `test_sites_handler_build_mode_identical_to_refine` no longer asserts the
#      specialist's tool id, because which edit tool a Build-mode turn names depends
#      on the site's engine. It pins what it was really about: the Build side of the
#      toggle is byte-identical to unset.
# The new section at the bottom pins the per-engine content (react must not command
# the ripple edit path, must not claim the page runs without JavaScript, must not
# promise a live url off an async publish, and must carry the prerender contract;
# html must admit it has no edit tool; ripple must be unchanged), the shared half on
# every branch including unknown, the content-keyed cache, and — derived from the MCP
# tool schemas — that every tool id these branches name is one the agent can call.
# Mutations: tests/mutations/sites_refine_engine_fork.json (11, all caught).
#
# These tests make the awareness DETERMINISTIC: every live /sites mode, on every
# engine, must carry the concierge block. They also pin the two honest LIMITS of
# that block, because an over-promising preamble would trade a blind agent for a
# lying one:
#   * it must NOT claim a create/draft already has a concierge (provisioning is
#     live-publish-only — `sites/service.py::_embed_concierge_bar` runs after the
#     build, and a preview returns from `publish` long before it);
#   * it must NOT claim the agent can configure the concierge's catalog/actions
#     (owner-authored only — no agent tool declares them; the known gap).

_CONCIERGE_MODES: list[tuple[str, SurfaceMeta]] = [
    ("create/html", SurfaceMeta(route_path="/sites")),
    ("create/svelte", SurfaceMeta(route_path="/sites", engine="svelte")),
    ("create/ripple", SurfaceMeta(route_path="/sites", engine="ripple")),
    (
        "refine",
        SurfaceMeta(route_path="/sites/site-abc", pocket_id="pkt-c", mode="build"),
    ),
    (
        "chat",
        SurfaceMeta(route_path="/sites/site-abc", pocket_id="pkt-c", mode="chat"),
    ),
]


@pytest.mark.parametrize("label,meta", _CONCIERGE_MODES, ids=[m[0] for m in _CONCIERGE_MODES])
async def test_every_sites_mode_knows_the_concierge_exists(label: str, meta: SurfaceMeta) -> None:
    """DETERMINISM: the concierge block is present in EVERY live /sites mode.

    Not "usually" and not on one engine — the reported bug was intermittent
    precisely because no mode carried it, so this is parametrized across all
    five dispatched (engine, mode) combinations rather than spot-checking one.
    """
    lower = (await sites_handler.build_preamble(WORKSPACE, USER, meta)).text.lower()

    assert "concierge" in lower, f"{label}: preamble never mentions the concierge"
    # Named as the thing the visitor actually sees on the page.
    assert "paw bar" in lower or "paw-bar" in lower, f"{label}: the bar is not named"
    # It must say the concierge answers the SITE's visitors, not the builder.
    assert "visitor" in lower, f"{label}: no visitor framing"


@pytest.mark.parametrize("label,meta", _CONCIERGE_MODES, ids=[m[0] for m in _CONCIERGE_MODES])
async def test_concierge_block_never_promises_a_configuration_tool(
    label: str, meta: SurfaceMeta
) -> None:
    """The block must not invent authority the agent does not have.

    Widget `catalog` and `actions` are owner-authored only — no agent tool
    declares them — so the preamble has to route the user to the dashboard
    instead of naming a tool that would hard-error.
    """
    lower = (await sites_handler.build_preamble(WORKSPACE, USER, meta)).text.lower()

    # No fabricated tool ids for concierge configuration.
    for phantom in (
        "mcp__pocketpaw_sites_manager__configure_concierge",
        "mcp__pawbar_actions__",
        "set_concierge",
        "configure_concierge",
    ):
        assert phantom not in lower, f"{label}: preamble names a non-existent tool {phantom!r}"
    # It points at where the owner really configures it.
    assert "dashboard" in lower, f"{label}: no pointer to the owner-facing surface"


async def test_create_ties_the_concierge_to_publish_not_to_the_draft() -> None:
    """A DRAFT has no concierge — provisioning is live-publish-only.

    `sites/service.py::_embed_concierge_bar` (and the `ensure_site_widget`
    trigger inside it) runs between the build and the deploy of a LIVE publish;
    a preview returns from `publish_pocket` long before it. Since the create
    flow is draft-first, an agent that told the user "your site has a concierge"
    right after a create would be wrong. The create block must tie the concierge
    to publishing.
    """
    for engine in (None, "svelte", "ripple"):
        meta = SurfaceMeta(route_path="/sites", engine=engine)
        lower = (await sites_handler.build_preamble(WORKSPACE, USER, meta)).text.lower()
        # The concierge arrives WITH the publish, not with the draft.
        assert "publish" in lower
        concierge_at = lower.index("concierge")
        window = lower[concierge_at : concierge_at + 700]
        assert "publish" in window, (
            f"engine={engine}: the concierge block does not tie provisioning to publish"
        )


async def test_concierge_awareness_does_not_depend_on_the_mcp_tool_id_import() -> None:
    """Awareness must survive the degraded profile path.

    `surface_registry._load_mcp_tool_ids` degrades to all-`None` (no MCP
    restriction) when the EE agent-layer import fails, and it MEMOIZES that
    result for the life of the process. Preamble knowledge must not be coupled
    to that cache, or a poisoned memo would take the concierge block with it.
    """
    import pocketpaw_ee.cloud.surface.surface_registry as registry

    original = registry._MCP_TOOL_IDS_CACHE
    try:
        registry._MCP_TOOL_IDS_CACHE = registry._McpToolIds(
            loaded=False,
            foresight_allow=None,
            sites_allow=None,
            studio_allow=None,
            belt_allow=None,
        )
        preamble = await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites")
        )
        assert "concierge" in preamble.text.lower()
    finally:
        registry._MCP_TOOL_IDS_CACHE = original


# ---------------------------------------------------------------------------
# RX-3 — the react EDIT lane reaches the preambles that route to it
# ---------------------------------------------------------------------------


async def test_react_create_step_routes_follow_up_changes_to_the_edit_tool() -> None:
    """The reported bug in prompt form: with no edit tool named, a follow-up
    "shorten the hero headline" left the agent one available move — a second
    ``create_react_site`` — which mints a SECOND site pocket and leaves the site the
    user is looking at unchanged. The react build step now names the edit tool and
    forbids the re-create.

    THE MUTATION THAT BREAKS THIS: delete the CHANGES-GO-THROUGH-THE-EDIT-TOOL
    paragraph from the react ``build_step``.
    """
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine="react")
        )
    ).text

    assert "mcp__pocketpaw_sites_manager__edit_react_component" in preamble
    lower = preamble.lower()
    # It says the re-create is wrong, and says WHY (a second site).
    assert "second `create_react_site`" in preamble or "second create_react_site" in lower
    assert "second site" in lower
    # And it teaches the two-call add-a-section shape the tool actually needs.
    assert "create=true" in lower
    assert "src/app.tsx" in lower


async def test_react_refine_names_the_edit_tool_and_not_the_ripple_merge(mongo_db: object) -> None:
    """A react site's refine chat must route to ``edit_react_component``.

    The default refine preamble names ``mcp__pocketpaw_pocket_specialist__edit``,
    which merges a rippleSpec — and a react pocket has no rippleSpec, so that is an
    instruction with nothing to act on. Per pocketpaw/CLAUDE.md, naming an
    existing-but-WRONG tool is the same defect as naming an absent one: the model
    does not error, it improvises.

    RETARGETED 2026-08-11 (fix/sites-refine-preamble-engine-fork). This test used to
    pass ``engine="react"`` in the meta and reach ``_react_refine_preamble`` through
    the RX-3 fork. The refine surface never sends ``engine`` — the
    SurfaceMetaProvider in paw-enterprise stamps site_id / pocket_id /
    focus_node_id / mode — so that fork rendered for nobody and this test was the
    only thing exercising it. It now goes through the branch a real turn takes: the
    engine resolved from the pocket. The prohibition assertion changed with it: the
    branch forbids "the pocket specialist" by concept rather than by tool id, which
    is how ``_create_preamble``'s react branch already words it, so the id must be
    ABSENT here rather than present-as-a-prohibition.

    THE MUTATION THAT BREAKS THIS: point the react branch's edit step at
    ``mcp__pocketpaw_pocket_specialist__edit``. Run: caught. (Applied 2026-08-11.)
    """
    user_id, pocket_id = await _seed_site_pocket("react")

    preamble = (
        await sites_handler.build_preamble(WORKSPACE, user_id, _refine_meta(pocket_id))
    ).text

    assert "mcp__pocketpaw_sites_manager__edit_react_component" in preamble
    # The pocket the agent must edit is named, so it cannot address the wrong one.
    assert pocket_id in preamble
    # The rippleSpec merge is refused — by concept, without handing the model the
    # tool id it would otherwise pattern-match.
    assert "do NOT call the pocket specialist" in preamble
    assert "pocket_specialist__edit" not in preamble
    # The ripple WIDGET vocabulary is absent: those rules are about a spec this
    # engine does not have, and carrying them would teach a react author to look
    # for widgets that are not there.
    assert "pricing-table" not in preamble
    # Re-creating is explicitly refused.
    assert "create_react_site" in preamble


async def test_react_refine_carries_the_prerender_and_write_scope_rules(mongo_db: object) -> None:
    """The two rules that replace the ripple widget rules on this engine.

    The prerender rule is the same hazard in React spelling (``useEffect`` does not
    run at prerender time, so a resting state set only in an effect bakes as the
    initial value), and the write scope is what the tool actually enforces — an edit
    naming a reserved path is rejected, so the preamble must not let the agent
    discover that by trial.

    RETARGETED 2026-08-11: same reason as the test above — through the pocket, not
    through a meta hint the surface does not send.
    """
    user_id, pocket_id = await _seed_site_pocket("react")

    preamble = (
        await sites_handler.build_preamble(WORKSPACE, user_id, _refine_meta(pocket_id))
    ).text
    lower = preamble.lower()

    assert "prerender" in lower
    assert "useeffect" in lower
    # The write scope + the reserved shell, spelled out.
    assert "src/" in preamble and "public/" in preamble
    assert "package.json" in preamble
    assert "src/paw/" in preamble
    # The dependency list, so the agent does not author an import it cannot install.
    assert "no way to add a dependency" in lower
    # And that the edit is a DRAFT, so the agent does not announce a live change.
    assert "draft" in lower
    assert "nothing is built and nothing goes live" in lower


async def test_ripple_refine_keeps_the_rippleSpec_merge(mongo_db: object) -> None:
    """The fork must not disturb the engine it was not written for.

    REPLACES ``test_non_react_refine_is_unchanged``, which asserted that None,
    "ripple" AND "svelte" all get the rippleSpec merge preamble. That was true only
    because the RX-3 fork keyed on a meta field the surface never sends: a svelte
    site has a ``source`` map and its own ``edit_svelte_component``, so handing it
    the specialist was the same defect as handing it to react. Ripple is the engine
    that genuinely keeps the merge path, and it is asserted through the pocket.
    """
    user_id, pocket_id = await _seed_site_pocket("ripple")

    preamble = (
        await sites_handler.build_preamble(WORKSPACE, user_id, _refine_meta(pocket_id))
    ).text

    assert "mcp__pocketpaw_pocket_specialist__edit" in preamble
    assert "edit_react_component" not in preamble
    assert "edit_svelte_component" not in preamble


# --- The refine engine fork (fix/sites-refine-preamble-engine-fork) -----------
#
# THE BUG: `_refine_preamble` was written for ripple and shipped to all four
# engines. On a react / html / svelte site it commanded
# `pocket_specialist__edit` — which merges a rippleSpec those pockets do not have
# (their content is a `source` map) — told the agent the page renders with no
# JavaScript (react ships a hydrating client bundle by default), claimed the site
# "auto-publishes from its source pocket", and then listed five SSR rules about
# ripple WIDGET shapes at a page with no widgets in it.
#
# It was invisible for the reason pocketpaw/CLAUDE.md gives under "The prompt may
# not command a tool the agent doesn't have": the agent does not raise on an
# unsatisfiable instruction, it improvises, and the improvisation reads like a
# normal reply. The specialist IS reachable here (`pocketpaw_pocket_specialist`
# rides `ALWAYS_ALLOWED_MCP_SERVERS`), so this is that rule's "naming an
# existing-but-wrong tool is the same defect" clause rather than a missing tool.
#
# TWO KINDS OF TEST BELOW, and the split matters:
#
#   1. RESOLUTION — that the branch is chosen from the POCKET. These seed a real
#      pocket through the pockets service and go through `build_preamble`, because
#      the whole defect is reachable only via the live path: `meta.engine` is a
#      create hint the refine surface never stamps, so a fork on it would have put
#      every refine on the `or "html"` default while every unit test that passed
#      an engine directly reported success.
#   2. CONTENT — what each branch may and may not say. These call
#      `_refine_preamble(meta, engine)` directly, so an engine's rules can be
#      pinned without a DB round trip per assertion.

REACT_TOOL = "mcp__pocketpaw_sites_manager__edit_react_component"
SVELTE_TOOL = "mcp__pocketpaw_sites_manager__edit_svelte_component"
HTML_TOOL = "mcp__pocketpaw_sites_manager__edit_html_file"
RIPPLE_TOOL = "mcp__pocketpaw_pocket_specialist__edit"
SOURCE_MAP_ENGINES = ("svelte", "react", "html")
ALL_REFINE_ENGINES = ("ripple", "svelte", "react", "html", None)


async def _seed_site_pocket(engine: str, *, workspace: str = WORKSPACE) -> tuple[str, str]:
    """Seed a real site pocket on ``engine``; return ``(user_id, pocket_id)``."""
    from pocketpaw_ee.cloud.models.user import User as _UserDoc
    from pocketpaw_ee.cloud.pockets import service as pockets_service
    from pocketpaw_ee.cloud.pockets.dto import CreatePocketRequest

    user = _UserDoc(
        email=f"owner-{engine}-{workspace}@sites.test",
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="Site Owner",
        active_workspace=workspace,
    )
    await user.insert()
    user_id = str(user.id)
    source = None if engine == "ripple" else {"src/App.tsx": "export default () => null;"}
    pocket = await pockets_service.create(
        workspace,
        user_id,
        CreatePocketRequest(
            name=f"{engine} site",
            type="site",
            pattern="landing",
            engine=engine,
            source=source,
        ),
    )
    return user_id, pocket["_id"]


async def _registered_mcp_tool_ids() -> set[str]:
    """Every ``mcp__<server>__<tool>`` id the agent layer actually registers.

    Walks the ``agent/mcp_servers`` package's ``build_*`` factories AND the pocket
    specialist, whose server is built in ``agent/pocket_specialist/mcp_tool.py`` —
    outside the package, and the one the refine ripple branch names. Two return
    shapes exist (``(name, server)`` from the package, a bare server dict from the
    specialist) so both are normalized here.

    A builder that raises is SKIPPED rather than failed on: this helper answers
    "which ids are real", and a server that cannot be built in a test process
    (missing SDK, missing credentials) cannot answer either way. The caller
    asserts the derivation found something, so a wholesale failure still shows up.
    """
    import importlib
    import pkgutil

    import pocketpaw_ee.agent.mcp_servers as servers_pkg
    from mcp import types

    module_names = [
        f"{servers_pkg.__name__}.{m.name}"
        for m in pkgutil.iter_modules(servers_pkg.__path__)
        if not m.name.startswith("_")
    ]
    module_names.append("pocketpaw_ee.agent.pocket_specialist.mcp_tool")

    registered: set[str] = set()
    for module_name in module_names:
        try:
            module = importlib.import_module(module_name)
        except Exception:  # pragma: no cover — see the docstring
            continue
        for attr in dir(module):
            if not attr.startswith("build_"):
                continue
            builder = getattr(module, attr)
            if not callable(builder):
                continue
            try:
                built = builder()
                if not built:
                    continue
                if isinstance(built, tuple):
                    server_name, server = built
                else:
                    server, server_name = built, built.get("name")
                handler = server["instance"].request_handlers[types.ListToolsRequest]
                listed = await handler(types.ListToolsRequest(method="tools/list"))
            except Exception:  # pragma: no cover
                continue
            registered |= {f"mcp__{server_name}__{tool.name}" for tool in listed.root.tools}
    return registered


@pytest.mark.parametrize("engine,expected_tool", [("react", REACT_TOOL), ("ripple", RIPPLE_TOOL)])
async def test_refine_reads_the_engine_off_the_pocket(
    mongo_db: object, engine: str, expected_tool: str
) -> None:
    """THE TEST THE FIX EXISTS FOR: the branch comes from the pocket's engine.

    The meta carries NO engine, exactly as the live refine surface sends it, so
    this fails on any implementation that forks on ``meta.engine`` — which is what
    the obvious fix would have done, and it would have handed a react site the html
    branch while looking correct.

    THE MUTATION THAT BREAKS THIS: make ``build_preamble`` pass ``meta.engine``
    instead of the resolved engine. Run: react got the unknown branch, the react
    edit tool was absent and this failed. (Applied 2026-08-11.)
    """
    user_id, pocket_id = await _seed_site_pocket(engine)

    preamble = (
        await sites_handler.build_preamble(WORKSPACE, user_id, _refine_meta(pocket_id))
    ).text

    assert f'engine="{engine}"' in preamble
    assert expected_tool in preamble


async def test_refine_ignores_a_stale_create_engine_hint(mongo_db: object) -> None:
    """The POCKET wins over ``meta.engine``, which is a create-time preset hint.

    ``surface_registry._sites_profile`` already states the precedence ("a pocket_id
    present means refine even if engine=svelte"), and it has to hold here too: a
    hint left over from the gallery's preset picker must never pick the edit tool
    for a site that was authored on another track.
    """
    user_id, pocket_id = await _seed_site_pocket("ripple")

    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE, user_id, _refine_meta(pocket_id, engine="react")
        )
    ).text

    assert 'engine="ripple"' in preamble
    assert RIPPLE_TOOL in preamble
    assert REACT_TOOL not in preamble


async def test_refine_on_an_unreadable_pocket_commands_no_edit_tool(mongo_db: object) -> None:
    """A pocket we cannot read yields "identify the engine first", not a guess.

    Every engine's edit path rejects a pocket from another track, so on an unknown
    engine the honest move is to make the agent look. The branch may LIST the
    per-engine tools (it is a routing table the agent resolves after reading the
    pocket) but must not hand it one as the tool to call now, and must not offer a
    publish for a change it has not established it can make.
    """
    user_id, _ = await _seed_site_pocket("ripple")

    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE, user_id, _refine_meta("ffffffffffffffffffffffff")
        )
    ).text
    lower = preamble.lower()

    assert 'engine="unknown"' in preamble
    assert "could not be determined" in lower
    # It routes through the pocket READ first.
    assert "mcp__pocketpaw_pocket__get_pocket" in preamble
    assert "mcp__pocketpaw_sites_manager__publish" not in preamble


async def test_refine_does_not_read_an_engine_from_another_workspace(mongo_db: object) -> None:
    """Tenancy: ``pockets_service.get`` gates by owner / shared_with / visibility and
    NOT by workspace, so a user in two workspaces could stamp a pocket from B in a
    chat in A. The engine — and with it the tool the prompt names — must not be read
    across that line.

    THE MUTATION THAT BREAKS THIS: drop the ``pocket.get("workspace") !=
    workspace_id`` guard in ``_refine_engine``. Run: the react branch rendered
    inside workspace A and this failed. (Applied 2026-08-11.)
    """
    user_id, pocket_id = await _seed_site_pocket("react", workspace="ws-other-tenant")

    preamble = (
        await sites_handler.build_preamble(WORKSPACE, user_id, _refine_meta(pocket_id))
    ).text

    # Not react's branch: the engine was refused, not read. (The unknown branch's
    # routing table still LISTS every engine's tool, which is the point of it —
    # what must not appear is react's branch, i.e. its engine tag and its rules.)
    assert 'engine="react"' not in preamble
    assert 'engine="unknown"' in preamble
    assert "could not be determined" in preamble.lower()
    assert "react-dom/server" not in preamble


async def test_react_refine_does_not_command_the_ripple_edit_path() -> None:
    """A react pocket has a ``source`` map and no rippleSpec, so the specialist's
    merge path has nothing to act on. The branch names react's own tool instead.

    THE MUTATION THAT BREAKS THIS: point the react branch's edit step at
    ``mcp__pocketpaw_pocket_specialist__edit``. Run: the forbidden id was present
    and this failed. (Applied 2026-08-11.)
    """
    preamble = sites_handler._refine_preamble(_refine_meta(), "react")

    assert "pocket_specialist__edit" not in preamble
    assert REACT_TOOL in preamble
    # And it says why, so the agent does not reach for the specialist off its own
    # prior about how pockets are edited.
    assert "do NOT call the pocket specialist" in preamble
    assert "NOT a rippleSpec" in preamble


async def test_react_refine_never_says_the_page_runs_without_javascript() -> None:
    """React sites ship their client bundle by default
    (``sites_keep_client_bundle_default``), so the ripple branch's "no JavaScript
    runs for the visitor" is simply false here — and it is the kind of false that
    makes an agent refuse a menu toggle the site can actually run."""
    preamble = sites_handler._refine_preamble(_refine_meta(), "react")
    lower = preamble.lower()

    assert "no javascript runs for the visitor" not in lower
    assert "renders statically" not in lower
    # It states the real shape: prerendered AND hydrating.
    assert "prerendered" in lower
    assert "client bundle" in lower


async def test_react_refine_does_not_promise_a_live_url_on_publish() -> None:
    """A react publish QUEUES a build (``sites/service.py::build_runs_async``), so
    the response is not a finished deploy: ``_enqueue_static_build`` creates the Site
    doc with ``deployed=False`` / ``url=""`` on a first publish, and a re-publish
    keeps the PREVIOUS deploy's url serving the pre-change page while the rebuild
    runs. "Show the live url" is wrong in both directions.

    THE MUTATION THAT BREAKS THIS: make ``_publish_runs_async`` return False. Run:
    the react branch lost the queued-build paragraph and the status tool, and this
    failed. (Applied 2026-08-11.)
    """
    preamble = sites_handler._refine_preamble(_refine_meta(), "react")
    lower = preamble.lower()

    assert "asynchronous" in lower
    assert "returns BEFORE the build starts" in preamble
    # #1920 shipped the read-only status tool, so the branch names it rather than
    # gesturing at the app. It is the ONLY way to learn how a react build ended.
    assert "mcp__pocketpaw_sites_manager__get_site_build_status" in preamble
    assert "build_reason" in preamble
    # The gate is `is_live` and not `deployed` / a non-empty url, either of which is
    # set while a rebuild serves the PREVIOUS page. Asserted as the operative
    # INSTRUCTION rather than as the presence of the field name: the first version of
    # this test asserted `` `is_live` `` appeared somewhere and "never report a site
    # as live off `deployed`", and a mutation that rewrote the gate's opening
    # sentence escaped both — the surviving body still contained each fragment.
    assert "`url` only when `is_live` is true" in preamble
    assert "never report a site as live off `deployed`" in lower
    # And the inversion the mutation introduced, named directly.
    assert "whenever `deployed` is true" not in preamble


async def test_react_refine_carries_the_prerender_contract() -> None:
    """The rule that actually binds a react edit, stated as
    ``pocketpaw-create-react-site/SKILL.md`` states it: ``<App />`` is rendered by
    ``react-dom/server`` at build time, ``useEffect`` does not run then, and
    ``window``/``document`` do not exist during that render — so a component must
    return its resting state in markup."""
    preamble = sites_handler._refine_preamble(_refine_meta(), "react")

    assert "react-dom/server" in preamble
    assert "`useEffect` does NOT run at prerender time" in preamble
    assert "RETURNED MARKUP" in preamble


async def test_react_refine_states_the_write_scope_the_tool_enforces() -> None:
    """The reserved paths come from the module the TOOL checks, not from prose.

    ``sites/react_paths.py`` is shared by ``edit_react_component`` and
    ``create_react_site``, so deriving the expectation from those constants means a
    fifth reserved file cannot land with the prompt still listing four. A prompt that
    under-lists does not fail loudly — the agent writes the file, gets
    ``site_edit.reserved_path``, and burns a turn learning what it should have been
    told.

    THE MUTATION THAT BREAKS THIS: hardcode ``_react_write_scope``'s list back into
    prose, dropping ``paw-prerender.mjs``. Run: the derived expectation still wanted
    it and this failed. (Applied 2026-08-11.)
    """
    from pocketpaw_ee.sites.react_paths import (
        REACT_AUTHORABLE_PREFIXES,
        REACT_RESERVED_FILES,
        REACT_RESERVED_PREFIX,
    )

    preamble = sites_handler._refine_preamble(_refine_meta(), "react")

    for reserved in REACT_RESERVED_FILES:
        assert f"`{reserved}`" in preamble, f"reserved path {reserved!r} not stated"
    assert f"`{REACT_RESERVED_PREFIX}`" in preamble
    for authorable in REACT_AUTHORABLE_PREFIXES:
        assert f"`{authorable}`" in preamble, f"authorable prefix {authorable!r} not stated"
    # Normalization happens before the check, so a literal-string reading of the list
    # is a loophole the agent must not think it has.
    assert "normalized" in preamble


@pytest.mark.parametrize("engine", SOURCE_MAP_ENGINES)
async def test_source_map_refine_drops_the_ripple_widget_rules(engine: str) -> None:
    """The five SSR rules name widget types, and a source-map page has no widgets.

    Shipping them to react/html/svelte was not merely useless: "pricing-table uses
    tiers" and "never the accordion widget" describe a widget catalog the agent
    cannot use here, so following them means inventing something.

    THE MUTATION THAT BREAKS THIS: append "Keep the 5 static-site (SSR) rules:
    `pricing-table` uses `tiers`" to ``_REFINE_SHARED_RULES``. Run: the ripple rules
    reached all three source-map branches and this failed on each. (Applied
    2026-08-11.)
    """
    preamble = sites_handler._refine_preamble(_refine_meta(), engine)

    # Matched on the RULE, not on the bare word: react's prerender rule legitimately
    # mentions an accordion's open panel as a `useState` initial value, and a test
    # that banned the word would have forced that guidance out to keep itself green.
    for widget_rule in (
        "`pricing-table` uses `tiers`",
        "NEVER the `accordion` widget",
        "NEVER the `form` or `newsletter` widget",
        "`hero+grid`",
        "`on_click` handler",
        "5 static-site (SSR) rules",
    ):
        assert widget_rule not in preamble, (
            f"{engine}: ripple widget rule {widget_rule!r} leaked onto a source-map engine"
        )


@pytest.mark.parametrize("engine", ALL_REFINE_ENGINES)
async def test_every_refine_branch_keeps_what_transfers(engine: str | None) -> None:
    """The engine-independent half, pinned on every branch including unknown.

    A fork is a place for four copies of an instruction to drift, so the shared
    rules are asserted across all of them rather than spot-checked on the one that
    was edited last.

    THE MUTATION THAT BREAKS THIS: drop ``f"{rules}"`` from ``_refine_preamble``'s
    return. Run: every branch lost the anchor-CTA rule and this failed on all five.
    (Applied 2026-08-11.)
    """
    preamble = sites_handler._refine_preamble(_refine_meta(), engine)
    lower = preamble.lower()

    # The source pocket is still threaded through every branch — a refine that
    # cannot name its pocket cannot act on it.
    assert REFINE_POCKET in preamble
    assert 'mode="refine"' in preamble
    # ASK-DON'T-ASSUME and its mechanism (refine keeps ripple_mode="on" on every
    # engine, so the widget is real here).
    assert "ask, don't assume" in lower
    assert "ask-user-questions" in preamble
    assert "fabricate" in lower
    # The funnel, real copy, anchor CTAs, and the flat lead form.
    assert "hero" in lower
    assert "pricing" in lower
    assert "footer" in lower
    assert "href" in lower
    assert "flat" in lower
    # Never reframed as a create or a dashboard pocket.
    assert "do NOT create a new site or a new pocket" in preamble
    assert "dashboard pocket" in lower
    # And the concierge block, on every branch.
    assert "concierge" in lower


async def test_svelte_refine_names_its_own_tool_and_calls_the_edit_a_draft() -> None:
    """``edit_svelte_component`` STAGES A DRAFT PREVIEW — it returns
    ``status:"draft"`` / ``is_live:false`` and the user clicks Submit for review.
    (The mcp server's module header still says "and republishes"; that has been
    stale since feat/sites-diff-edit.) An agent told the edit republished would
    announce a change that is not live.

    THE MUTATION THAT BREAKS THIS: retitle the block "THE EDIT REPUBLISHES THE
    SITE". Run: it ESCAPED the first time — ``"draft" in lower`` still held because
    the surviving payload mentions ``status:"draft"`` — which is why the assertions
    below are separate and "republish" is banned outright. Re-run: caught.
    (Applied 2026-08-11.)
    """
    preamble = sites_handler._refine_preamble(_refine_meta(), "svelte")
    lower = preamble.lower()

    assert SVELTE_TOOL in preamble
    assert "pocket_specialist__edit" not in preamble
    assert "draft" in lower
    # Both halves asserted separately, and "republish" banned outright. An `or`
    # across these let a mutation that retitled the block "THE EDIT REPUBLISHES THE
    # SITE" escape: the surviving `status:"draft"` mention kept the test green while
    # the heading — the sentence the agent actually acts on — said the opposite.
    assert "not a deploy" in lower
    assert "is not the live site" in lower
    assert "republish" not in lower
    # Both edit shapes, so the agent prefers the diff over a whole-file rewrite.
    assert "old_string" in preamble
    assert "new_source" in preamble


async def test_html_refine_routes_the_edit_to_the_html_edit_tool() -> None:
    """html refine applies the change with ``edit_html_file``.

    This test used to assert the OPPOSITE — that the branch admits it has no edit
    tool — and it was correct when written. ``edit_html_file`` shipped in db083bfc
    without touching the handler, so the assertion outlived the gap it described
    and certified the stale prompt as correct on every run. Rewritten rather than
    deleted: the html branch still has rules the other engines do not, and they are
    pinned below.

    THE MUTATION THAT BREAKS THIS: point the html branch's edit step back at
    ``mcp__pocketpaw_pocket__get_pocket`` + "hand the user the markup". Run: the
    branch stopped naming a write tool and this failed.
    """
    preamble = sites_handler._refine_preamble(_refine_meta(), "html")
    lower = preamble.lower()

    assert HTML_TOOL in preamble
    # The html argument is `file_path` — react's `component_path` is a different
    # tool's schema and a wrong-argument call is rejected.
    assert "`file_path`" in preamble
    assert "component_path" not in preamble
    # Root-relative paths: the `src/` prefix belongs to the react track.
    assert "root-relative" in lower
    # The wrong tools stay unnamed: create mints a SECOND site, and the specialist
    # merges a rippleSpec this pocket does not have.
    assert "pocket_specialist__edit" not in preamble
    assert SVELTE_TOOL not in preamble
    assert REACT_TOOL not in preamble
    # `create_html_site` IS named, but only as the thing never to call for a change.
    assert "NEVER call `create_html_site` again to apply a change" in preamble
    # The edit stages a draft, so the branch must not report it live.
    assert "not published" in lower
    assert "is_live:false" in preamble


async def test_html_refine_can_offer_to_publish_the_change() -> None:
    """A saved html edit is publishable, so the branch must say how.

    While html had no edit tool the publish step was deliberately withheld: naming
    one would have invited the agent to publish the LAST build as if it carried a
    change it never applied. That reasoning expires with the gap — withholding it
    now strands the user with a saved draft and no route to live.

    THE MUTATION THAT BREAKS THIS: restore ``engine in (None, "html")`` on the
    ``publish_step`` gate. Run: html saved a change it could not take live and this
    failed.
    """
    preamble = sites_handler._refine_preamble(_refine_meta(), "html")

    assert "mcp__pocketpaw_sites_manager__publish" in preamble
    # Publishing stays the user's call — an edit must not auto-publish.
    assert "the user's call" in preamble.lower()
    # html builds INLINE (`build_runs_async` is react-only), so it must NOT carry
    # react's queued-build paragraph or the status tool that only async needs.
    assert "get_site_build_status" not in preamble
    assert "asynchronous" not in preamble.lower()


async def test_the_unknown_engine_step_no_longer_calls_html_uneditable() -> None:
    """The engine-lookup fallback listed html as the engine with no edit tool.

    It is reached whenever the pocket read fails, so its stale half sent every
    unresolvable html site down the same dead end the html branch did.

    THE MUTATION THAT BREAKS THIS: put "html has no edit tool at all yet" back.
    Run: the fallback denied a registered tool and this failed.
    """
    step = sites_handler._refine_unknown_engine_step("pkt-1")

    assert HTML_TOOL in step
    assert "no edit tool" not in step.lower()
    # It still names the other three, and still makes the agent look first.
    assert SVELTE_TOOL in step and REACT_TOOL in step and RIPPLE_TOOL in step
    assert "mcp__pocketpaw_pocket__get_pocket" in step


async def test_ripple_refine_is_unchanged_apart_from_the_publish_claim() -> None:
    """The ripple branch was always correct, so the fork must not have rewritten it.

    Pins the five widget rules and the Tier-0 animation list that only apply here,
    plus the one thing that DID change: the old text said the site "auto-publishes
    from its source pocket", which was never true — publish is a tool call and on a
    paid tier it can open a checkout.

    THE MUTATION THAT BREAKS THIS: delete rule 2 (``pricing-table`` uses ``tiers``)
    from the ripple branch. Run: the fork had quietly rewritten the one engine that
    was already correct and this failed. (Applied 2026-08-11.)
    """
    preamble = sites_handler._refine_preamble(_refine_meta(), "ripple")
    lower = preamble.lower()

    assert RIPPLE_TOOL in preamble
    assert "tiers" in lower
    assert "accordion" in lower
    assert "tier-0" in lower
    assert "hero+grid" in lower
    # The corrected publish claim.
    assert "auto-publish" not in lower
    assert "the user's call" in lower
    # ripple publishes INLINE, so its response is already conclusive: no queued-build
    # paragraph and no status tool. That absence is what makes the
    # `_publish_runs_async` mutation catchable in both directions.
    assert "get_site_build_status" not in preamble
    assert "asynchronous" not in lower
    # It still gates on `is_live` — that field is on every publish response, and
    # `deployed` alone lies during a rebuild on any engine that grows one.
    assert "`url` only when `is_live` is true" in preamble


async def test_refine_cache_key_is_the_rendered_digest() -> None:
    """Refine reads the pocket, so ``meta_key`` can no longer describe it: two sites
    on different engines render different instructions from the same meta shape.
    ``content_key`` moves exactly when the rendered text moves.

    THE MUTATION THAT BREAKS THIS: key refine on ``meta_key("sites",
    meta.pocket_id)``. Run: the key stopped tracking the rendered text and this
    failed. (Applied 2026-08-11.)
    """
    from pocketpaw_ee.cloud.surface.handlers._helpers import content_key

    rendered = await sites_handler.build_preamble(WORKSPACE, USER, _refine_meta())

    assert rendered.cache_key == content_key("sites", rendered.text)
    # A create meta still answers the meta key (it reads nothing about the site).
    create = await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    assert create.cache_key.startswith("sites:/sites")


async def test_every_tool_a_refine_branch_names_is_a_registered_tool() -> None:
    """The gate pocketpaw/CLAUDE.md asks for, applied to this surface.

    ``tests/test_prompt_names_only_real_tools.py`` does this for the inline ripple
    prompt and says outright that it covers nothing else. The refine fork now names
    a different tool per engine, which is exactly the shape that drifts: a tool
    moves servers or gets renamed, and the prompt keeps commanding the old id while
    the agent improvises instead of erroring.

    Derived from the MCP tool schemas, never a hand-kept list.
    """
    import re

    registered = await _registered_mcp_tool_ids()
    assert registered, "no MCP tools enumerated — the derivation broke, not the prompt"
    # The derivation has to reach the specialist's server, which lives OUTSIDE the
    # mcp_servers package. Asserted rather than assumed: without it the ripple
    # branch's tool would read as unregistered and the check would fail on the one
    # branch that was never broken.
    assert RIPPLE_TOOL in registered, "the enumeration missed the pocket specialist"

    named: set[str] = set()
    for engine in ALL_REFINE_ENGINES:
        text = sites_handler._refine_preamble(_refine_meta(), engine)
        named |= set(re.findall(r"mcp__[a-z0-9_]+__[a-z0-9_]+", text))

    unresolved = named - registered
    # ``edit_react_component`` lands with feat/sites-react-edit-lane, which this
    # branch depends on and must merge after. The exemption is self-clearing: it
    # only tolerates ABSENCE, so the branch that registers the tool turns this into
    # an ordinary pass with nothing to remove.
    assert unresolved <= {REACT_TOOL}, (
        "a refine branch names tools that no MCP server registers — the agent "
        f"cannot call these and will improvise instead of erroring: {sorted(unresolved)}"
    )


# --- HE-10 follow-through (fix/sites-html-refine-names-the-edit-tool) ---------
#
# THE REPORTED BUG: on an html site's refine chat the agent answered an edit
# request with "editing an html site's files from chat is not wired up yet" and
# handed back a code block instead of applying the change.
#
# It was never a missing tool. ``edit_html_file`` shipped in db083bfc wired all
# the way through — service, MCP tool, ``sites_manager`` registration,
# ``SITES_TOOL_IDS``, the /sites allow-list — but that commit never touched
# ``handlers/sites.py``. Compare 34582e73, which shipped the react edit tool AND
# +137 lines of this handler in the same commit. So the prompt kept the pre-HE-10
# text and the agent believed the prompt over its own tool list.
#
# It shipped clean because the gate below ran one way only:
# ``test_every_tool_a_refine_branch_names_is_a_registered_tool`` catches a
# preamble naming a tool that does not exist, and nothing caught a tool that
# exists and no preamble names.
#
# The tests above pin the fixed branch. The one below is the missing direction of
# the gate, and it is the one that would have caught this on the day it landed.


async def test_every_registered_edit_tool_is_named_by_its_refine_branch() -> None:
    """The INVERSE gate: a registered edit tool no preamble names is unreachable.

    The failure has no error to notice — the agent simply reports the capability
    as missing, which is indistinguishable from the product genuinely lacking it.
    Derived from the MCP tool schemas, never a hand-kept list, so a fourth edit
    tool joins this gate by being registered rather than by being remembered.

    THE MUTATION THAT BREAKS THIS: drop ``edit_html_file`` from the html refine
    branch. Run: a registered tool went unnamed and this failed.
    """
    registered = await _registered_mcp_tool_ids()
    assert registered, "no MCP tools enumerated — the derivation broke, not the prompt"

    for engine, tool_id in (
        ("svelte", SVELTE_TOOL),
        ("react", REACT_TOOL),
        ("html", HTML_TOOL),
    ):
        text = sites_handler._refine_preamble(_refine_meta(), engine)
        assert tool_id in registered, f"{tool_id} is not registered — fix the server"
        assert tool_id in text, (
            f"{tool_id} is a REGISTERED tool that the {engine} refine branch never "
            "names, so the agent cannot reach it and will tell the user the "
            "capability does not exist"
        )


async def test_html_create_routes_follow_up_changes_to_the_edit_tool() -> None:
    """The create side had the same hole: html got no "changes go through the edit
    tool" clause, so a follow-up change in the SAME conversation re-created the
    site. react has carried this clause since RX-3; html now does too.

    THE MUTATION THAT BREAKS THIS: delete the clause from the html build step.
    Run: the create branch let a follow-up change mint a second site and this
    failed.
    """
    preamble = await _preamble_for(None)  # no engine hint == the html default

    assert HTML_TOOL in preamble
    assert "CHANGES GO THROUGH THE EDIT TOOL" in preamble
    assert "SAME pocket_id" in preamble
    # Create is still the FIRST thing named — this is a create preamble.
    assert preamble.index("create_html_site") < preamble.index("edit_html_file")
