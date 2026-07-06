# sites.py — /sites surface preamble.
#
# Created: 2026-06-02 — Orients the chat agent when the user is on the /sites
# surface (the Paw Sites gallery + describe-to-create rail). Without it the
# surface fell back to GENERIC and the agent built + talked "pocket" instead of
# a publishable website (the operator-reported "agent builds pockets not sites"
# drift). Static orientation — no live data to fake.
#
# Updated: 2026-06-03 (pm) — Point the procedure back at the `pocketpaw-create-site`
# skill now that bundled skills actually load on the SDK backend. The earlier
# note here ("skills can't load under setting_sources=[]") is obsolete: the
# claude_agent_sdk backend now loads the bundled skills as a Claude Code local
# plugin via the SDK `plugins=` option (see `bundled_skills_plugin_dir` +
# `settings.sdk_load_bundled_skills`), so the agent can invoke the skill by
# natural-language intent — no slash command, no setting_sources change. The
# skill carries the full create→publish flow (build the source pocket via
# create-pocket, publish via the sites-manager tool, surface the live URL, relay
# errors), so the preamble PREFERS it and keeps the raw MCP tools only as a
# fallback for when the skill is unavailable (e.g. sdk_load_bundled_skills off).
# The rail still sends the user's description as PLAIN TEXT; intent invocation
# does not need a slash.
# Updated: 2026-06-03 (feat/sites-landing-brain) — Point the preamble at the new
# `pocketpaw-create-paw-site` marketing brain (the dedicated landing-page
# author) instead of the generic create-site path, and stamp the source pocket
# `type="site"` + `pattern="landing"`. Dropped the `form-layout` lead-form nudge:
# the `form`/`newsletter` widgets emit a nested `<form>` that is invalid inside
# the static site template's outer POST form, so the published page captured zero
# leads (the broken "Option A" render). The lead form must be FLAT native
# `input`/`button{type:submit}` with real `name=`. create-site Path A (publish an
# existing pocket) is unchanged; only a brand-new-site description routes here.
# Updated: 2026-06-04 (feat/sites-refine-surface) — The /sites surface now has
# TWO modes. The gallery (no pocket_id) keeps the create-a-new-site preamble
# above. The per-site refine chat at /sites/[siteId] stamps `pocket_id` (the
# site's source pocket) + `site_id` in the surface meta; when `pocket_id` is
# present, `build_preamble` branches to a LANDING-AWARE REFINE preamble that
# tells the agent to EDIT the existing published pocket via
# `mcp__pocketpaw_pocket_specialist__edit` — never rebuild from scratch, never
# treat it as a dashboard pocket — while preserving the landing structure and
# the same 5 SSR rules the create brain enforces. Refine-mode rules mirror
# `src/pocketpaw/bundled_skills/_bundled/skills/pocketpaw-create-paw-site/SKILL.md`.
# Updated: 2026-06-04 (feat/sites-svelte-engine) — the CREATE branch now forks on
# `meta.engine` ("ripple" | "svelte"), set by the /sites create UI's "Use Svelte
# pages" toggle. `engine="svelte"` returns a parallel create preamble that
# PREFERS the `pocketpaw-create-svelte-site` skill (the Svelte-track authoring
# brain — it writes hand-written SvelteKit components, no rippleSpec/catalog) and
# points the MCP fallback at `mcp__pocketpaw_sites_manager__create_svelte_site`.
# Every other engine value (None / "ripple") keeps the existing ripple marketing
# brain (`pocketpaw-create-paw-site` + create_landing_site) byte-for-byte. The
# refine branch (keyed on `pocket_id`) is untouched by the toggle.
# Updated: 2026-07-06 (feat/sites-crew-create-flow, SC-crew) — the CREATE branch
# now optionally runs a guided two-phase authoring-crew flow behind the
# `settings.sites_crew_enabled` flag (default OFF). When the flag is ON, a create
# meta returns `_crew_create_preamble`: PHASE 1 assesses the request's clarity
# and interviews the user (one round of 3–5 questions) when it is vague, with a
# "just build it" escape hatch; PHASE 2 retrieves a design system
# (`mcp__pocketpaw_design_systems__*`), pulls real assets (stock/icons/palette
# MCP tools), states a one-line brief, and BUILDS via the same engine skill the
# single-shot path uses (`pocketpaw-create-svelte-site` on svelte,
# `pocketpaw-create-paw-site` on ripple) — the skill still owns the SSR page
# assembly, the crew preamble only feeds it the design system, sections, copy,
# and assets. When the flag is OFF, the create branch returns
# `_create_preamble(meta)` byte-for-byte (no behaviour change). The refine/chat
# branches (keyed on `pocket_id`) are untouched.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the /sites surface preamble.

    Two modes, keyed on whether the meta carries a ``pocket_id``:

    * **Create** (no ``pocket_id``) — the /sites gallery / describe-to-create
      rail. Build AND publish a brand-new marketing site.
    * **Refine** (``pocket_id`` present) — the per-site chat at
      ``/sites/[siteId]``. Refine the EXISTING published site by editing its
      source pocket in place; never rebuild from scratch.
    """
    if meta.pocket_id:
        return _refine_preamble(meta)
    if _crew_enabled():
        return _crew_create_preamble(meta)
    return _create_preamble(meta)


def _crew_enabled() -> bool:
    """Read the ``sites_crew_enabled`` flag (default OFF).

    Isolated so a config-import failure can never break the create path — a
    bad read falls back to the shipped single-shot builder, not a 5xx. Mirrors
    the ``get_settings()`` access pattern in ``handlers/foresight.py``.
    """
    try:
        from pocketpaw.config import get_settings

        return bool(getattr(get_settings(), "sites_crew_enabled", False))
    except Exception:
        return False


def _create_preamble(meta: SurfaceMeta) -> str:
    """The /sites gallery preamble — build AND publish a brand-new site.

    Two engines, keyed on ``meta.engine`` (the "Use Svelte pages" toggle):

    * ``"svelte"`` — the Svelte track. Prefer the
      ``pocketpaw-create-svelte-site`` skill (hand-written SvelteKit
      components, prerendered static) and point the MCP fallback at
      ``create_svelte_site``.
    * anything else (``None`` / ``"ripple"``) — the default ripple marketing
      brain, unchanged.
    """
    if meta.engine == "svelte":
        return _svelte_create_preamble(meta)
    route = meta.route_path or "/sites"
    return (
        f'<surface kind="sites" route="{route}" />\n'
        "<sites-orientation>\n"
        "The user is on the SITES surface, building a publishable WEBSITE that "
        "deploys as a standalone static page on the edge — not an in-app pocket "
        "dashboard. It renders as a real marketing landing page read top to "
        "bottom as a conversion funnel: nav, hero, services, social proof, "
        "pricing, a call-to-action, a lead-capture form, footer. Talk about it as "
        "a 'site' or 'page' — never a 'pocket'. The pocket is only the source "
        "spec; it auto-publishes to a live URL. The page is rendered STATICALLY "
        "(no JavaScript runs for the visitor), so favor clean marketing copy, "
        "real sections, anchor-link CTAs, and a working lead form over generic "
        "dashboard / KPI widgets.\n"
        "</sites-orientation>\n"
        "<sites-procedure>\n"
        "Treat the user's message on this surface as a request to BUILD AND "
        "PUBLISH a marketing site. PREFER the `pocketpaw-create-paw-site` skill — "
        "invoke it by intent (no slash command needed). It is the dedicated "
        "marketing brain: it composes the page by conversion role, stamps the "
        'source pocket `type="site"` + `pattern="landing"`, and then publishes '
        "it and shows the live URL. Critically, the lead-capture form must be "
        'FLAT native `input`/`textarea`/`button{type:"submit"}` widgets with '
        "real field names (name, email, phone, message) — NEVER the `form` or "
        "`newsletter` widget, which nests an invalid `<form>` inside the site "
        "template's outer POST form and captures zero leads. Pricing uses "
        "`pricing-table` with `tiers`, CTAs are anchor links, and any animation "
        "stays CSS-only (Tier-0).\n"
        "If that skill is unavailable, fall back directly with the MCP tools: "
        "call `mcp__pocketpaw_pocket_specialist__create` to build the "
        'conversion-ordered landing spec (stamp `type="site"` + '
        '`pattern="landing"`, flat named lead inputs, `pricing-table` tiers, '
        "anchor CTAs), then `mcp__pocketpaw_sites_manager__publish` with the "
        "returned pocket_id.\n"
        "Either way: relay any publish error — never claim a phantom publish — and "
        "after it succeeds, SHOW the live `url` plus a link to /sites where the "
        "user manages their sites. Keep talking 'site' / 'page', never 'pocket'.\n"
        "</sites-procedure>"
    )


def _svelte_create_preamble(meta: SurfaceMeta) -> str:
    """The /sites create preamble on the SVELTE track (``engine="svelte"``).

    Parallel to the ripple ``_create_preamble`` body, but the deliverable is
    authored as hand-written SvelteKit components rather than a ripple widget
    spec. Prefers the ``pocketpaw-create-svelte-site`` skill (which owns the
    component-authoring how-to) and points the MCP fallback at
    ``create_svelte_site``. The orientation framing — publishable static
    website, conversion funnel, talk 'site' not 'pocket' — matches the ripple
    path so the toggle only changes the authoring brain, not the goal.
    """
    route = meta.route_path or "/sites"
    return (
        f'<surface kind="sites" route="{route}" engine="svelte" />\n'
        "<sites-orientation>\n"
        "The user is on the SITES surface with the Svelte engine selected ('Use "
        "Svelte pages'), building a publishable WEBSITE that deploys as a "
        "standalone static page on the edge — not an in-app pocket dashboard. It "
        "renders as a real marketing landing page read top to bottom as a "
        "conversion funnel: nav, hero, services, social proof, pricing, a "
        "call-to-action, a lead-capture form, footer. Talk about it as a 'site' "
        "or 'page' — never a 'pocket'. The pocket is only the source; it "
        "auto-publishes to a live URL. On this track the page is built from "
        "hand-written SvelteKit components and PRERENDERED to static HTML (no "
        "JavaScript runs for the visitor on first paint), so favor premium "
        "marketing copy, real sections, anchor-link CTAs, and a working lead form "
        "over generic dashboard / KPI widgets.\n"
        "</sites-orientation>\n"
        "<sites-procedure>\n"
        "Treat the user's message on this surface as a request to BUILD AND "
        "PUBLISH a marketing site on the Svelte track. This track is MANDATORY - author the page as hand-written SvelteKit components, never ripple widgets. Use the"  # noqa: E501
        "`pocketpaw-create-svelte-site` skill — invoke it by intent (no slash "
        "command needed). It is the dedicated Svelte-track authoring brain: YOU "
        "write premium hand-written SvelteKit components (Hero, Pricing, Faq, …) "
        "at the design quality bar, assemble them into the source map, and it "
        'persists the source pocket `type="site"` + `pattern="landing"` + '
        '`engine="svelte"` and then publishes it and shows the live URL. There '
        "is NO rippleSpec and NO widget catalog on this track — do not draft a "
        "rippleSpec, do not call `get_widget_spec`, do not use the pocket "
        "specialist, and ABSOLUTELY DO NOT call `mcp__pocketpaw_sites_manager__create_landing_site` or the `pocketpaw-create-paw-site` skill - those build a RIPPLE widget site, which is the WRONG output for this track. The Svelte component files ARE the page. The ONLY sanctioned create tool on this track is `create_svelte_site`; if you cannot use it, STOP and say so rather than falling back to any ripple/landing tool. The create-svelte-site "  # noqa: E501
        "skill owns the authoring how-to (the source-map shape and the "
        "resting-state SSR rule — render the final state in markup, never only in "
        "`onMount`, because prerender bakes the resting frame).\n"
        "If that skill is unavailable, fall back directly with the MCP tools: "
        "author the SvelteKit source map yourself, then call "
        "`mcp__pocketpaw_sites_manager__create_svelte_site` with the `source` "
        'object (it stamps `type="site"` + `pattern="landing"` + '
        '`engine="svelte"` and persists the pocket), then '
        "`mcp__pocketpaw_sites_manager__publish` with the returned pocket_id.\n"
        "Either way: relay any publish error — never claim a phantom publish — and "
        "after it succeeds, SHOW the live `url` plus a link to /sites where the "
        "user manages their sites. Keep talking 'site' / 'page', never 'pocket'.\n"
        "</sites-procedure>"
    )


def _crew_create_preamble(meta: SurfaceMeta) -> str:
    """The /sites create preamble on the guided authoring-crew flow.

    Gated behind ``settings.sites_crew_enabled`` (default OFF). Same visible
    behaviour as a design agent that asks questions and then builds, realized
    as ONE agent running two staged phases behind the proven build pipeline:

    * **Phase 1** — a clarity gate. Assess whether the request already covers
      the design dimensions; if it does, restate and proceed; if it is vague,
      ask ONE round of 3–5 high-value questions and stop (always with a "just
      build it" escape hatch).
    * **Phase 2** — retrieve/adapt a design system, gather real assets, state a
      one-line brief, then BUILD via the SAME engine skill the single-shot path
      uses. The skill owns the SSR page assembly + rules; the crew preamble only
      feeds it the chosen design system, sections, copy, and assets.

    The BUILD step forks on ``meta.engine`` exactly like ``_create_preamble``:
    ``"svelte"`` → the ``pocketpaw-create-svelte-site`` skill +
    ``create_svelte_site``; anything else → the ``pocketpaw-create-paw-site``
    skill + the ripple create/publish path.
    """
    route = meta.route_path or "/sites"
    is_svelte = meta.engine == "svelte"
    engine_attr = ' engine="svelte"' if is_svelte else ""
    engine_note = (
        " On this track the page is authored as hand-written SvelteKit "
        "components and PRERENDERED to static HTML (no JavaScript runs for the "
        "visitor on first paint)."
        if is_svelte
        else " The page is rendered STATICALLY (no JavaScript runs for the visitor)."
    )
    if is_svelte:
        build_step = (
            "BUILD via the `pocketpaw-create-svelte-site` skill — invoke it by "
            "intent (no slash command). It is the Svelte-track authoring brain: "
            "YOU write the premium hand-written SvelteKit components (Hero, "
            "Pricing, Faq, …) at the design quality bar, THEME them with the "
            "chosen design system's tokens + your asset URLs, and it persists "
            'the source pocket `type="site"` + `pattern="landing"` + '
            '`engine="svelte"` and publishes it. There is NO rippleSpec and NO '
            "widget catalog on this track. If the skill is unavailable, author "
            "the source map yourself and call "
            "`mcp__pocketpaw_sites_manager__create_svelte_site` then "
            "`mcp__pocketpaw_sites_manager__publish`. The skill owns the "
            "component assembly and the resting-state SSR rule — defer to it "
            "for those; your job is to hand it the design system, sections, "
            "copy, and real assets."
        )
    else:
        build_step = (
            "BUILD via the `pocketpaw-create-paw-site` skill — invoke it by "
            "intent (no slash command). It is the marketing brain: it composes "
            'the page by conversion role, stamps the source pocket `type="site"` '
            '+ `pattern="landing"`, and publishes it. THEME the page with the '
            "chosen design system's tokens + your asset URLs. If the skill is "
            "unavailable, fall back to "
            "`mcp__pocketpaw_pocket_specialist__create` (build the "
            "conversion-ordered landing spec) then "
            "`mcp__pocketpaw_sites_manager__publish`. The skill owns the page "
            "assembly and the static-site (SSR) rules — defer to it for those; "
            "your job is to hand it the design system, sections, copy, and real "
            "assets."
        )
    return (
        f'<surface kind="sites" route="{route}"{engine_attr} mode="crew" />\n'
        "<sites-orientation>\n"
        "The user is on the SITES surface, building a publishable WEBSITE that "
        "deploys as a standalone static page on the edge — not an in-app pocket "
        "dashboard. It renders as a real marketing landing page read top to "
        "bottom as a conversion funnel: nav, hero, services, social proof, "
        "pricing, a call-to-action, a lead-capture form, footer. Talk about it "
        "as a 'site' or 'page' — never a 'pocket'. The pocket is only the "
        f"source spec; it auto-publishes to a live URL.{engine_note} You are "
        "the AUTHORING CREW: rather than building blindly from one line, you "
        "run a guided two-phase flow — first make sure you understand the "
        "request, then design and build a coherent, on-brand site.\n"
        "</sites-orientation>\n"
        "<sites-procedure>\n"
        "Run TWO phases. Do not skip Phase 1, and do not start building until "
        "Phase 1 is resolved.\n"
        "\n"
        "PHASE 1 — CLARITY GATE + INTERVIEW.\n"
        "Assess the user's request across these dimensions: (a) the purpose / "
        "goal of the site, (b) the business and its audience, (c) the key "
        "sections they want, (d) brand — colors, vibe, any existing logo or "
        "brand assets, (e) voice / tone.\n"
        "- IF the request ALREADY covers most of these dimensions (a detailed "
        "prompt) → do NOT interrogate. Briefly restate the plan in one or two "
        "lines and proceed straight to Phase 2.\n"
        "- IF the request is VAGUE (e.g. 'make me a site for my cafe') → ask "
        "3–5 SHORT, high-value questions in ONE message covering the biggest "
        "gaps: what the site should achieve, the vibe or brands they admire, "
        "the must-have sections, and any brand colors. THEN STOP and wait for "
        "the reply — do not build yet. Always offer an out: end with '…or say "
        "\"just build it\" and I'll pick sensible defaults.' NEVER ask more "
        "than ONE round of questions; if the user already gave detail, or "
        "replies, or says 'just build it', move on to Phase 2 immediately.\n"
        "\n"
        "PHASE 2 — DESIGN + BUILD.\n"
        "1. PICK A LOOK. Call "
        "`mcp__pocketpaw_design_systems__list_design_systems` to see the "
        "library, choose the best fit by industry + aesthetic, then "
        "`mcp__pocketpaw_design_systems__get_design_system` with its slug to "
        "load the DESIGN.md + tokens.css. THEME the site with those tokens — "
        "colors, type scale, spacing, component styles — and honor the "
        "system's rationale and anti-patterns.\n"
        "2. CUSTOM COLORS. If the user gave a brand color, call "
        "`mcp__pocketpaw_palette__scale_from_color` with the hex to get a full "
        "scale and OVERRIDE the design system's primary with it. If they gave "
        "a logo or reference-image URL, call "
        "`mcp__pocketpaw_palette__extract_palette` on it and key the palette "
        "off that.\n"
        "3. REAL ASSETS. Use `mcp__pocketpaw_stock__search_stock_images` for "
        "hero and section photography and `mcp__pocketpaw_icons__search_icons` "
        "for feature icons. Wire the REAL returned URLs into the page — never "
        "leave placeholders or invent image paths.\n"
        "4. BRIEF. State a one-line brief back so the user sees the plan — e.g. "
        "'Building a [design-system vibe] site for [business] with sections "
        "[…], palette [primary].' Then build.\n"
        f"5. {build_step}\n"
        "6. PUBLISH and SHOW the live `url` plus a link to /sites where the "
        "user manages their sites.\n"
        "\n"
        "ROBUSTNESS (this flow must never stall or disappoint in real use):\n"
        "- If ANY tool errors (design-system, stock, palette, or icons), do "
        "NOT retry blindly or stall. Proceed with sensible defaults — a "
        "reasonable built-in look, generic-but-tasteful section imagery — and "
        "briefly note what you fell back on. A tool failure must NEVER block "
        "the build.\n"
        "- ONE round of questions maximum. If the user already gave detail or "
        "says 'just build it', skip straight to Phase 2 with sensible "
        "defaults.\n"
        "- Never claim a publish that didn't happen — relay the real publish "
        "error and show the real `url`. No phantom URLs.\n"
        "- Keep the 'site' / 'page' vocabulary throughout; never say 'pocket'.\n"
        "</sites-procedure>"
    )


def _refine_preamble(meta: SurfaceMeta) -> str:
    """The /sites/[siteId] refine preamble — edit an EXISTING published site.

    Landing-aware: mirrors the create-paw-site brain's structure + 5 SSR rules
    so an edit can't reintroduce a static-site trap. Carries the source
    ``pocket_id`` so the agent edits the right pocket in place.
    """
    route = meta.route_path or "/sites"
    pocket_id = meta.pocket_id or ""
    return (
        f'<surface kind="sites" route="{route}" pocket="{pocket_id}" mode="refine" />\n'
        "<sites-orientation>\n"
        f"The user is REFINING an EXISTING published Paw Site (source pocket "
        f"`{pocket_id}`) — a live standalone marketing website already deployed "
        "as a static page on the edge. They are on its per-site chat, asking for "
        "a CHANGE to that page. Do NOT rebuild the site from scratch, do NOT "
        "create a new site or a new pocket, and do NOT treat it as an in-app "
        "dashboard pocket. It is a real marketing landing page that reads top to "
        "bottom as a conversion funnel: nav, hero, services, social proof, "
        "pricing, a call-to-action, a flat lead-capture form, footer. Talk about "
        "it as a 'site' or 'page' — never a 'pocket'. The page renders STATICALLY "
        "(no JavaScript runs for the visitor), so every change must still work as "
        "plain HTML.\n"
        "</sites-orientation>\n"
        "<sites-procedure>\n"
        "Treat the user's message as an edit to APPLY to the existing site, then "
        f"re-publish. Apply the change to pocket `{pocket_id}` via "
        "`mcp__pocketpaw_pocket_specialist__edit` (the merge/edit path — it "
        "mutates the existing spec in place). NEVER use the create path and NEVER "
        "rebuild the page from scratch; a refine is a targeted edit on top of the "
        "current landing spec. After the edit lands it can be re-published (the "
        "site auto-publishes from its source pocket); relay any publish error — "
        "never claim a phantom publish — and show the live `url`.\n"
        "PRESERVE the landing structure (nav → hero → services → proof → pricing "
        "→ flat lead form → footer) and keep the 5 static-site (SSR) rules intact "
        "while you edit:\n"
        "1. Lead capture stays FLAT native `input`/`textarea`/"
        '`button{type:"submit"}` with real field names (name, email, phone, '
        "message) — NEVER the `form` or `newsletter` widget, which nests an "
        "invalid `<form>` inside the site template's outer POST form and captures "
        "zero leads.\n"
        "2. `pricing-table` uses `tiers` (never `plans`/`columns`).\n"
        "3. An FAQ is `heading` + `text` pairs — NEVER the `accordion` widget "
        "(its panels only open with JS, so on a static site the answers never "
        "expand).\n"
        "4. Every CTA is an anchor `href` (or `tel:` / `mailto:`) — never an "
        "`on_click` handler, which is a dead button with no client JS.\n"
        "5. `hero` is the marketing Hero widget — never the dashboard "
        "`hero+grid` (a page-header plus a KPI `stat` grid); no metric grid, no "
        "charts. This is marketing, not analytics.\n"
        "Any animation stays Tier-0 (CSS-only, static-safe) — `aurora`, "
        "`marquee`, `border-beam`, `shimmer`, `text-effect`; never `reveal`, "
        "`parallax`, or `spotlight` (they need client JS and hide content on a "
        'static page). Keep `type="site"` + `pattern="landing"` on the pocket. '
        "Keep talking 'site' / 'page', never 'pocket'.\n"
        "</sites-procedure>"
    )


__all__ = ["build_preamble", "_crew_create_preamble"]
