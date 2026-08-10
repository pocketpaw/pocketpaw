# tests/cloud/surface/test_sites_handler.py — Sites surface handler.
#
# Created: 2026-06-03 — Guards the /sites surface preamble.
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


async def test_sites_handler_specifies_flat_lead_capture_form() -> None:
    """Every create carries the flat-native lead-form rule so the published
    static site captures leads: named fields (email) built FLAT, never a nested
    form widget. Checked on both the html default and the ripple track."""
    for engine in (None, "ripple"):
        preamble = (
            await sites_handler.build_preamble(
                WORKSPACE, USER, SurfaceMeta(route_path="/sites", engine=engine)
            )
        ).text
        lower = preamble.lower()
        # A lead-capture form is part of the marketing/landing build.
        assert "form" in lower
        # At least one concrete, named field so leads are actually captured.
        assert "email" in lower
        # The flat-native rule.
        assert "flat" in lower


# --- Refine mode (meta carries a pocket_id — the per-site /sites/[siteId] chat) ---

REFINE_POCKET = "pkt-existing-site-123"


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
    """Refine applies the change via the merge/edit path on the existing pocket,
    not the create path — so the published site is updated in place."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE,
            USER,
            SurfaceMeta(route_path="/sites/site-abc", pocket_id=REFINE_POCKET, site_id="site-abc"),
        )
    ).text

    # The edit/merge tool, not a fresh create.
    assert "mcp__pocketpaw_pocket_specialist__edit" in preamble
    # It should not steer the agent to spin up a brand-new pocket from scratch.
    assert "from scratch" in preamble.lower()  # named as the thing to avoid


async def test_sites_handler_refine_mode_is_landing_aware() -> None:
    """The refine preamble carries the same landing structure + 5 SSR rules as
    the create brain, so an edit can't introduce a static-site trap."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE,
            USER,
            SurfaceMeta(route_path="/sites/site-abc", pocket_id=REFINE_POCKET, site_id="site-abc"),
        )
    ).text

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


async def test_create_names_design_and_asset_tools() -> None:
    """The create preamble names the design-system, stock, and custom-color tool
    ids so the agent themes the site and wires real assets."""
    out = (
        await sites_handler.build_preamble(WORKSPACE, USER, SurfaceMeta(route_path="/sites"))
    ).text

    assert "mcp__pocketpaw_design_systems__list_design_systems" in out
    assert "mcp__pocketpaw_design_systems__get_design_system" in out
    assert "mcp__pocketpaw_stock__search_stock_images" in out
    # Custom-color path: brand hex → full scale.
    assert "mcp__pocketpaw_palette__scale_from_color" in out


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
    output — the additive brief helper does not touch the refine dispatch."""
    meta = SurfaceMeta(route_path="/sites/site-abc", pocket_id=REFINE_POCKET, site_id="site-abc")
    live = (await sites_handler.build_preamble(WORKSPACE, USER, meta)).text

    assert live == sites_handler._refine_preamble(meta)
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
    # And it is the mutate-and-republish refine preamble (regression guard).
    assert "mcp__pocketpaw_pocket_specialist__edit" in build
    assert 'mode="refine"' in build


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


async def test_react_refine_names_the_edit_tool_and_not_the_ripple_merge() -> None:
    """A react site's refine chat must route to ``edit_react_component``.

    The default refine preamble names ``mcp__pocketpaw_pocket_specialist__edit``,
    which merges a rippleSpec — and a react pocket has no rippleSpec, so that is an
    instruction with nothing to act on. Per pocketpaw/CLAUDE.md, naming an
    existing-but-WRONG tool is the same defect as naming an absent one: the model
    does not error, it improvises.

    THE MUTATION THAT BREAKS THIS: drop the ``engine == "react"`` fork from
    ``_refine_preamble`` so react falls through to the ripple preamble.
    """
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE,
            USER,
            SurfaceMeta(
                route_path="/sites/site-abc",
                pocket_id=REFINE_POCKET,
                site_id="site-abc",
                engine="react",
            ),
        )
    ).text

    assert "mcp__pocketpaw_sites_manager__edit_react_component" in preamble
    # The pocket the agent must edit is named, so it cannot address the wrong one.
    assert REFINE_POCKET in preamble
    # The ripple merge path is named ONLY as a prohibition — naming it at all is
    # deliberate (the agent has the tool, so silence would leave it as a plausible
    # move), but it must never read as the instruction.
    assert "do NOT call `mcp__pocketpaw_pocket_specialist__edit`" in preamble
    # The ripple WIDGET vocabulary is absent: those rules are about a spec this
    # engine does not have, and carrying them would teach a react author to look
    # for widgets that are not there.
    assert "pricing-table" not in preamble
    assert "accordion" not in preamble
    # Re-creating is explicitly refused.
    assert "create_react_site" in preamble


async def test_react_refine_carries_the_prerender_and_write_scope_rules() -> None:
    """The two rules that replace the ripple widget rules on this engine.

    The prerender rule is the same hazard in React spelling (``useEffect`` does not
    run at prerender time, so a resting state set only in an effect bakes as the
    initial value), and the write scope is what the tool actually enforces — an edit
    naming a reserved path is rejected, so the preamble must not let the agent
    discover that by trial."""
    preamble = (
        await sites_handler.build_preamble(
            WORKSPACE,
            USER,
            SurfaceMeta(
                route_path="/sites/site-abc",
                pocket_id=REFINE_POCKET,
                site_id="site-abc",
                engine="react",
            ),
        )
    ).text
    lower = preamble.lower()

    assert "prerender" in lower
    assert "useeffect" in lower
    # The write scope + the reserved shell, spelled out.
    assert "src/" in preamble and "public/" in preamble
    assert "package.json" in preamble
    assert "src/paw/" in preamble
    # And that the edit is a DRAFT, so the agent does not announce a live change.
    assert "draft" in lower
    assert "does not publish" in lower


async def test_non_react_refine_is_unchanged() -> None:
    """The fork must not disturb the engine it was not written for: a refine with no
    engine hint, or a ripple one, still gets the rippleSpec merge preamble."""
    for engine in (None, "ripple", "svelte"):
        preamble = (
            await sites_handler.build_preamble(
                WORKSPACE,
                USER,
                SurfaceMeta(
                    route_path="/sites/site-abc",
                    pocket_id=REFINE_POCKET,
                    site_id="site-abc",
                    engine=engine,
                ),
            )
        ).text
        assert "mcp__pocketpaw_pocket_specialist__edit" in preamble, engine
        assert "edit_react_component" not in preamble, engine
