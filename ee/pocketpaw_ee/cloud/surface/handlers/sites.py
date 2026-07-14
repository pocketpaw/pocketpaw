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
# Updated: 2026-07-06 (feat/sites-crew-taste, SC-TASTE) — the crew svelte
# `build_step` now instructs the agent to author the components with the bundled
# `design-taste-svelte` skill (premium, anti-slop, static-safe styling) on top of
# the design system's tokens. Svelte-only: the ripple `build_step` is unchanged.
# Updated: 2026-07-06 (feat/sites-crew-frontend-brief, SC-2) — added
# `_frontend_preamble(meta, brief)`, the ADDITIVE brief-driven twin of
# `_create_preamble`. It renders the Frontend stage's build instructions FROM a
# structured `DesignBrief` (the crew's baton): it walks the ordered `sitemap`,
# injects the matching `copy` blocks + real `asset_manifest` imagery, threads the
# branding design-system tokens/voice, and ROUTES by `brief.engine` ("svelte" →
# `create_svelte_site`; "ripple" → `create_landing_site` / the pocket specialist,
# noting `create_dynamic_site` for `pattern="dynamic"`). It preserves the same
# static-site (SSR) rules the create/refine preambles enforce. `build_preamble`'s
# live create/refine dispatch is UNCHANGED — `_frontend_preamble` is exercised by
# tests now and wired into the flow later by the orchestration slice (SC-9) behind
# a flag (SC-11); it is exported in `__all__` so that slice can import it.
# Updated: 2026-07-04 (feat/sites-chat-mode, CHAT-BE) — the refine branch (keyed
# on `pocket_id`) now forks on `meta.mode` ("build" default | "chat"), set by the
# /sites/[siteId] Build/Chat toggle. `mode="chat"` routes to `_chat_preamble`, a
# NO-MUTATION Q&A preamble: the user is asking a QUESTION about the existing site;
# ANSWER helpfully but DO NOT call `pocket_specialist__edit`, do NOT modify or
# republish the site, and do NOT create pockets. `mode="build"` (or unset) keeps
# `_refine_preamble` byte-for-byte (today's mutate-and-republish behavior). The
# create branch (no `pocket_id`) ignores `mode`.
# Updated: 2026-07-14 (fix/sites-unified-create-preamble) — collapsed the THREE
# create preambles (`_create_preamble`, `_svelte_create_preamble`,
# `_crew_create_preamble`) + the `sites_crew_enabled` flag gate into ONE always-on
# `_create_preamble`. The clarity gate (Phase 1: assess → ask_user chips when
# vague, "just build it" escape hatch) and the design phase (Phase 2:
# design-system → palette → real assets → brief) are engine-agnostic instructions,
# so they run for EVERY create regardless of engine — this fixes the bug where a
# flaky flag left the agent on the no-questions single-shot preamble and it built a
# default design without asking. Only the final BUILD step forks on `meta.engine`:
# "html" (the NEW DEFAULT when engine is unset) → hand-authored static HTML/CSS via
# `create_html_site`; "svelte" → hand-written components via `create_svelte_site`;
# "ripple" → the widget landing spec via the pocket specialist (the one engine that
# does NOT author markup by hand). `_crew_enabled()` and the two removed preambles
# are gone; `sites_crew_enabled` in config is now vestigial. The refine/chat
# branches (keyed on `pocket_id`) are untouched.

from __future__ import annotations

from typing import Any

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.sites_crew.models import DesignBrief


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the /sites surface preamble.

    Modes, keyed on the meta:

    * **Create** (no ``pocket_id``) — the /sites gallery / describe-to-create
      rail. Build AND publish a brand-new marketing site.
    * **Chat** (``pocket_id`` present AND ``mode == "chat"``) — the per-site
      chat with the Build/Chat toggle set to Chat. Answer QUESTIONS about the
      existing site with NO mutation: never edit, republish, or create a pocket.
    * **Refine / Build** (``pocket_id`` present, ``mode`` "build" or unset) —
      the per-site chat at ``/sites/[siteId]``. Refine the EXISTING published
      site by editing its source pocket in place; never rebuild from scratch.
    """
    if meta.pocket_id:
        if meta.mode == "chat":
            return _chat_preamble(meta)
        return _refine_preamble(meta)
    return _create_preamble(meta)


def _create_preamble(meta: SurfaceMeta) -> str:
    """The /sites CREATE preamble — the single guided authoring flow for a
    brand-new site, across ALL engines.

    ONE preamble, always on (no feature flag): the clarity gate and the design
    phase are engine-agnostic instructions, so they apply whether the deliverable
    is plain HTML, SvelteKit, or a ripple widget spec. Only the final BUILD step
    forks on ``meta.engine``:

    * ``"html"`` (and the DEFAULT when no engine is set) — a plain static
      HTML/CSS bundle you hand-author → ``create_html_site``. No framework, no
      build step; publishing serves ``index.html`` directly on the edge.
    * ``"svelte"`` — hand-written SvelteKit components → ``create_svelte_site``.
    * ``"ripple"`` — a ripple widget landing spec via the pocket specialist →
      ``create_landing_site``. The ONE engine that does not author markup by hand.

    Phase 1 assesses the request and, when it is vague, asks ONE round of
    high-value questions via the ``ask_user`` chips (with a "just build it"
    escape hatch); Phase 2 picks a design system, gathers real assets, states a
    one-line brief, then builds via the engine-appropriate tool above.
    """
    route = meta.route_path or "/sites"
    engine = (meta.engine or "html").lower()
    if engine not in ("html", "svelte", "ripple"):
        engine = "html"
    engine_attr = f' engine="{engine}"'

    if engine == "svelte":
        engine_note = (
            " On this track the page is authored as hand-written SvelteKit "
            "components and PRERENDERED to static HTML (no JavaScript runs for the "
            "visitor on first paint)."
        )
        build_step = (
            "BUILD via the `pocketpaw-create-svelte-site` skill — invoke it by "
            "intent (no slash command). YOU write premium hand-written SvelteKit "
            "components (Hero, Pricing, Faq, …) at the design quality bar, "
            "authoring them with the `design-taste-svelte` skill for premium, "
            "non-generic styling on top of the chosen design system's tokens, "
            "THEME them with those tokens + your asset URLs, and it persists the "
            'source pocket `type="site"` + `pattern="landing"` + `engine="svelte"` '
            "and publishes it. There is NO rippleSpec and NO widget catalog on "
            "this track — do not draft a rippleSpec or call the pocket "
            "specialist. If the skill is unavailable, author the source map "
            "yourself and call `mcp__pocketpaw_sites_manager__create_svelte_site` "
            "then `mcp__pocketpaw_sites_manager__publish`."
        )
    elif engine == "ripple":
        engine_note = " The page is rendered STATICALLY (no JavaScript runs for the visitor)."
        build_step = (
            "BUILD via the `pocketpaw-create-paw-site` skill — invoke it by "
            "intent (no slash command). It composes the page by conversion role "
            'and stamps the source pocket `type="site"` + `pattern="landing"`. '
            "THEME the page with the chosen design system's tokens + your asset "
            "URLs. The lead-capture form must be FLAT native "
            '`input`/`textarea`/`button{type:"submit"}` widgets with real field '
            "names (name, email, phone, message) — NEVER the `form` or "
            "`newsletter` widget, which nests an invalid `<form>` on a static "
            "site and captures zero leads. If the skill is unavailable, fall back "
            "to `mcp__pocketpaw_pocket_specialist__create` (build the "
            "conversion-ordered ripple landing spec) then "
            "`mcp__pocketpaw_sites_manager__publish`. This is the ripple/widget "
            "track — the page is a widget spec, not hand-authored markup."
        )
    else:  # html (default)
        engine_note = (
            " On this track YOU hand-author the page as a plain static HTML/CSS "
            "bundle — no framework and no build step; publishing serves your "
            "`index.html` directly on the edge."
        )
        build_step = (
            "BUILD the page yourself as a clean, premium static HTML/CSS bundle. "
            "Author a root `index.html` (plus `styles.css`, and only the vanilla "
            "JS a static page genuinely needs) at the design quality bar, THEME it "
            "with the chosen design system's tokens + your asset URLs, then "
            "persist it by calling `mcp__pocketpaw_sites_manager__create_html_site` "
            'with the `source` map (it stamps the source pocket `type="site"` + '
            '`pattern="landing"` + `engine="html"`), then '
            "`mcp__pocketpaw_sites_manager__publish` with the returned pocket_id. "
            "There is NO rippleSpec and NO widget catalog on this track — do "
            "not draft a rippleSpec or call the pocket specialist; the HTML files "
            "ARE the page. The lead-capture form must be a real `<form>` with FLAT "
            "named `input`/`textarea` fields (name, email, phone, message) and a "
            '`button type="submit"`. `index.html` MUST contain the full resting '
            "state in markup (never rendered only by JS), since the visitor is "
            "served static HTML. This static-HTML track is the DEFAULT; only if "
            "the user EXPLICITLY asks for a Svelte/component build should you use "
            "`mcp__pocketpaw_sites_manager__create_svelte_site` instead, or for a "
            "live-data / dynamic app "
            "`mcp__pocketpaw_sites_manager__create_dynamic_site` — otherwise stay "
            "on static HTML."
        )

    # The Phase-1 clarity question renders as COMPLETE UI. On every engine except
    # svelte-create, inline ripple is ON (see surface_registry._sites_profile), so
    # the agent renders a real `ask-user-questions` ripple widget (a ```ui-spec
    # block) — a stepped, clickable question whose `completeActions` emit
    # `chat.send`, returning the user's pick as their next message. On the svelte
    # track ripple is OFF, so it falls back to the `ask_user` MCP tool (chips).
    ripple_on = engine != "svelte"
    if ripple_on:
        clarify_step = (
            "Render the question as an `ask-user-questions` RIPPLE WIDGET — a "
            "```ui-spec fenced block using the {version, ui} envelope, NOT plain "
            "text and NOT a tool call. It renders as clickable option chips and, "
            "on selection, sends the answer back as the user's next message. Use "
            "exactly this shape, tailored to the site (option `description` is "
            "optional):\n"
            "```ui-spec\n"
            '{"version": 1, "ui": {"type": "ask-user-questions", "props": '
            '{"questions": [{"title": "What visual style fits best?", "options": '
            '[{"title": "Clean & modern"}, {"title": "Warm & friendly"}, '
            '{"title": "Bold & confident"}, {"title": "Elegant & premium"}, '
            '{"title": "Just build it — you pick"}]}], "completeActions": '
            '{"action": "emit", "target": "chat.send"}}}}\n'
            "```\n"
            "`completeActions` MUST emit `chat.send`. Emit the widget, then END "
            "YOUR TURN and wait for the click — do NOT call any build tool."
        )
    else:
        clarify_step = (
            "Ask via the `mcp__pocketpaw_ask__ask_user` tool (this svelte track "
            "has inline ripple OFF, so the tool renders the chips): pass a "
            "one-line `question` and 3–5 short `options` (always include a 'Just "
            "build it — you pick' option). Call it, then END YOUR TURN and wait "
            "for the click — do NOT call any build tool."
        )

    return (
        f'<surface kind="sites" route="{route}"{engine_attr} mode="create" />\n'
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
        "ASK, DON'T ASSUME — this is the core rule of this surface. Whenever you "
        "need information you do not have, or you face a MATERIAL choice you would "
        "otherwise guess — the visual style, which sections to include, "
        "headline / CTA wording, real-world facts (the business's address, hours, "
        "phone, pricing, service list, team), a logo or brand assets, or which of "
        "several directions to take — ASK THE USER instead of inventing it. Ask "
        "the SAME complete-UI way Phase 1 asks the design direction below (a "
        "clickable question widget for CHOICES), and plain text for open-ended "
        "facts. NEVER fabricate "
        "real-world facts — no invented testimonials, statistics, prices, "
        "addresses, or contact details; if you don't know it, ask for it. Batch "
        "related questions into ONE widget, never re-ask something already "
        "answered, and ALWAYS include a 'you decide' / 'just build it' option so "
        "the user is never blocked. Proceed on your own ONLY when the user has "
        "explicitly told you to pick, or the detail is purely cosmetic.\n"
        "\n"
        "Run TWO phases. PHASE 1 IS MANDATORY AND COMES FIRST. On the user's "
        "first create message you MUST ask the design-direction question and "
        "then STOP — do NOT call any design, create, or publish tool in that "
        "same turn, and do NOT skip this even for a long, detailed brief. This "
        "rule OVERRIDES any skill, habit, or instinct that says build "
        "immediately; asking the question is the ONLY thing you do on the first "
        "turn.\n"
        "\n"
        "PHASE 1 — ASK THE DESIGN DIRECTION (always, as your first action).\n"
        "Ask for the VISUAL DIRECTION — the one thing a prompt almost never pins "
        f"down, so you must never assume it. {clarify_step}\n"
        "- ONLY skip the question if the user's message ALREADY names a specific "
        "visual STYLE (e.g. 'dark brutalist', 'like Stripe', 'minimal "
        "black-and-white') — in that case briefly restate the plan and go to "
        "Phase 2. A described business or section list is NOT a style and does "
        "NOT let you skip.\n"
        "- You are NOT limited to one round: per ASK, DON'T ASSUME, raise a fresh "
        "question WHENEVER a new material gap appears (missing content, a real "
        "fact, a branching choice) rather than guessing — but keep each question "
        "high-value and batch related asks. The moment the user picks 'just build "
        "it', stop asking and proceed with sensible defaults.\n"
        "\n"
        "PHASE 2 — DESIGN + BUILD.\n"
        "1. PICK A LOOK. Call "
        "`mcp__pocketpaw_design_systems__list_design_systems` to see the "
        "library, choose the best fit by industry + aesthetic, then "
        "`mcp__pocketpaw_design_systems__get_design_system` with its slug to "
        "load the DESIGN.md + tokens.css. THEME the site with those tokens — "
        "colors, type scale, spacing, component styles — and honor the "
        "system's rationale and anti-patterns. Do NOT default to the warm / "
        "earthy system just because the business is a cafe, salon, or local "
        "shop — that reflex makes every site look the same. Match the read, "
        "and when two systems fit, rotate to the less-obvious one. To keep "
        "even the same system from looking identical build-to-build, feel free "
        "to reseed its accent hue via step 2 (a considered, non-default color) "
        "even when the user gave no brand color.\n"
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
        "ASK, DON'T ASSUME: if the requested edit is ambiguous, or applying it "
        "needs a real fact or content you don't have (new copy, a price, a "
        "section's content, or which of several interpretations the user means), "
        "ASK with an `ask-user-questions` ripple widget (include a 'you decide' "
        "option) instead of guessing — and NEVER fabricate real-world facts "
        "(testimonials, stats, prices, addresses, contact details).\n"
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


def _render_copy(block: Any) -> str:
    """Flatten one section's copy block into an inline, LLM-readable string.

    ``brief.copy[section_id]`` is free-form: usually a dict of copy fields
    (``{"headline": ..., "subhead": ...}``), sometimes a plain string/list. Join
    dict entries as ``key: value`` so the actual copy text lands in the preamble.
    """
    if isinstance(block, dict):
        return "; ".join(f"{k}: {v}" for k, v in block.items())
    if isinstance(block, (list, tuple)):
        return "; ".join(str(item) for item in block)
    return str(block)


def _frontend_preamble(meta: SurfaceMeta, brief: DesignBrief) -> str:
    """Render the Frontend stage's build instructions FROM a structured brief.

    The crew's baton (``DesignBrief``) in → the build preamble out. This is the
    additive, brief-driven twin of ``_create_preamble``: instead of orienting the
    agent off a raw user message, it walks the brief's ordered ``sitemap``,
    injects the matching ``copy`` blocks and ``asset_manifest`` imagery, threads
    the branding design-system tokens/voice, and ROUTES by ``brief.engine``
    (``"svelte"`` → ``create_svelte_site``; ``"ripple"`` → ``create_landing_site``
    / the pocket specialist). It preserves the same static-site (SSR) rules the
    create/refine preambles enforce so a brief-driven build can't reintroduce a
    static-site trap.

    NOTE: NOT wired into ``build_preamble`` — the live create/refine dispatch is
    byte-for-byte unchanged. Exercised by tests now; the orchestration slice
    (SC-9) wires it in behind a flag (SC-11).
    """
    route = meta.route_path or "/sites"
    engine = brief.engine

    # --- Ordered sitemap → build-in-order instructions, with copy injected. ---
    section_lines: list[str] = []
    for i, section in enumerate(brief.sitemap, start=1):
        head = f"{i}. `{section.role}` section"
        if section.heading:
            head += f' — heading "{section.heading}"'
        section_lines.append(head)
        if section.notes:
            section_lines.append(f"   - notes: {section.notes}")
        copy_block = brief.copy.get(section.id)
        if copy_block:
            section_lines.append(f"   - copy: {_render_copy(copy_block)}")
    sitemap_block = (
        "\n".join(section_lines)
        if section_lines
        else "(the brief carries no explicit sitemap — build a standard "
        "conversion funnel: nav → hero → services → proof → pricing → cta → "
        "flat lead form → footer)"
    )

    # --- Real imagery from the asset manifest (never invent placeholder URLs). ---
    if brief.asset_manifest:
        asset_lines = "\n".join(
            f"- {a.kind}: {a.url}" + (f' (alt: "{a.alt}")' if a.alt else "")
            for a in brief.asset_manifest
        )
        assets_block = (
            "Use these REAL asset URLs from the brief's manifest verbatim — never "
            "invent placeholder image paths:\n" + asset_lines + "\n"
        )
    else:
        assets_block = ""

    # --- Design system + voice from the branding layer. ---
    branding = brief.branding
    design_block = ""
    ds = branding.design_system
    if ds is not None:
        design_block += f"Theme the page with the brief's design system (`{ds.name}`). "
        if ds.tokens_css:
            design_block += (
                "Apply its compiled `tokens_css` (CSS custom properties) as the "
                "single source of truth for color, type, spacing, radius, and "
                "elevation — never hard-code ad-hoc values. "
            )
        if ds.colors:
            design_block += (
                "Use its palette scales (the 50..900 steps per role color) for "
                "every surface, text, and accent. "
            )
        if ds.rationale:
            design_block += (
                "Honor the design rationale — its mood, do's/don'ts, and "
                f"anti-patterns: {ds.rationale} "
            )
    if branding.voice:
        design_block += f'Match this brand voice throughout the copy: "{branding.voice}". '

    # --- Route by engine. svelte → hand-written components; ripple → landing spec. ---
    if engine == "svelte":
        route_block = (
            "BUILD ENGINE: svelte. Author this page as hand-written SvelteKit "
            "components (the svelte track) — one component per section above, at "
            "the premium design quality bar. There is NO rippleSpec and NO widget "
            "catalog on this track: do not draft a rippleSpec, do not call the "
            "pocket specialist. Assemble the components into the source map and "
            "persist via `create_svelte_site` "
            "(`mcp__pocketpaw_sites_manager__create_svelte_site`), which stamps "
            'the source pocket `type="site"` + `pattern="landing"` + '
            '`engine="svelte"`; then `mcp__pocketpaw_sites_manager__publish`. Do '
            "NOT fall back to any ripple/landing create tool on this track."
        )
    else:
        route_block = (
            "BUILD ENGINE: ripple. Compose this page as a conversion-ordered "
            "ripple landing spec via the pocket specialist / `create_landing_site` "
            "(`mcp__pocketpaw_sites_manager__create_landing_site`), which stamps "
            'the source pocket `type="site"` + `pattern="landing"`; then '
            "`mcp__pocketpaw_sites_manager__publish` with the returned pocket_id."
        )
        if brief.pattern == "dynamic":
            route_block += (
                ' This brief is marked `pattern="dynamic"` (a live-data site): the '
                "dynamic create path is `create_dynamic_site` "
                "(`mcp__pocketpaw_sites_manager__create_dynamic_site`), but keep "
                "the static landing build as the primary focus."
            )

    return (
        f'<surface kind="sites" route="{route}" engine="{engine}" mode="frontend" />\n'
        "<sites-orientation>\n"
        "You are the FRONTEND stage of the site-authoring crew. Build a "
        "publishable WEBSITE from the structured brief below — a real marketing "
        "landing page that deploys as a standalone static page on the edge, NOT "
        "an in-app pocket dashboard. It reads top to bottom as a conversion "
        "funnel. Talk about it as a 'site' or 'page' — never a 'pocket'. The "
        "pocket is only the source spec; it auto-publishes to a live URL. The "
        "page renders STATICALLY (no JavaScript runs for the visitor), so every "
        "section must work as plain HTML.\n"
        "</sites-orientation>\n"
        "<sites-brief>\n"
        f"GOAL: {brief.goal}\n"
        + (f"AUDIENCE: {brief.audience}\n" if brief.audience else "")
        + "SITEMAP — build these sections IN THIS ORDER, using the copy provided:\n"
        + sitemap_block
        + "\n"
        + assets_block
        + (design_block + "\n" if design_block else "")
        + "</sites-brief>\n"
        "<sites-procedure>\n" + route_block + "\n"
        "PRESERVE the landing structure and keep the static-site (SSR) rules "
        "intact while you build:\n"
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
        "5. `hero` is the marketing Hero widget — never the dashboard `hero+grid` "
        "(a page-header plus a KPI `stat` grid); no metric grid, no charts. This "
        "is marketing, not analytics.\n"
        "Any animation stays Tier-0 (CSS-only, static-safe) — `aurora`, "
        "`marquee`, `border-beam`, `shimmer`, `text-effect`; never `reveal`, "
        "`parallax`, or `spotlight` (they need client JS and hide content on a "
        "static page).\n"
        "After it publishes, relay any publish error — never claim a phantom "
        "publish — and SHOW the live `url` plus a link to /sites. Keep talking "
        "'site' / 'page', never 'pocket'.\n"
        "</sites-procedure>"
    )


def _chat_preamble(meta: SurfaceMeta) -> str:
    """The /sites/[siteId] CHAT preamble — answer questions, NEVER mutate.

    The Build/Chat toggle is set to Chat: the user is on an existing site's
    chat asking a QUESTION about it (what's on the page, what a section says,
    how it's structured, why it looks a certain way). Answer helpfully, but this
    is a read-only surface — do NOT edit the site, do NOT republish it, and do
    NOT create a pocket. Mirrors the refine preamble's site/page vocabulary and
    orientation, minus every mutation instruction.
    """
    route = meta.route_path or "/sites"
    pocket_id = meta.pocket_id or ""
    return (
        f'<surface kind="sites" route="{route}" pocket="{pocket_id}" mode="chat" />\n'
        "<sites-orientation>\n"
        f"The user is CHATTING about an EXISTING published Paw Site (source pocket "
        f"`{pocket_id}`) — a live standalone marketing website already deployed as "
        "a static page on the edge. They are on its per-site chat with the "
        "Build/Chat toggle set to CHAT, so they are asking a QUESTION about the "
        "site, not requesting a change to it. It is a real marketing landing page "
        "that reads top to bottom as a conversion funnel: nav, hero, services, "
        "social proof, pricing, a call-to-action, a flat lead-capture form, "
        "footer. Talk about it as a 'site' or 'page' — never a 'pocket'.\n"
        "</sites-orientation>\n"
        "<sites-procedure>\n"
        "Treat the user's message as a QUESTION to ANSWER about the existing site "
        "— explain what is on the page, what a section says, how it is structured, "
        "or give advice — and answer clearly and concisely.\n"
        "This is a READ-ONLY surface. Do NOT modify or edit the site, do NOT "
        "call the pocket specialist edit/merge tool or any create tool, do NOT "
        "re-publish the site, and do NOT create a new pocket or a new "
        "site. If the user actually wants a CHANGE applied, tell them to switch "
        "the toggle to BUILD — in Chat mode you only answer questions and never "
        "touch the live page. Keep talking 'site' / 'page', never 'pocket'.\n"
        "</sites-procedure>"
    )


__all__ = ["build_preamble", "_create_preamble", "_frontend_preamble", "_chat_preamble"]
