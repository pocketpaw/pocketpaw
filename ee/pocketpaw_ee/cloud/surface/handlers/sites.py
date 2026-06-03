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

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the /sites surface preamble (static orientation + build procedure)."""
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
        "source pocket `type=\"site\"` + `pattern=\"landing\"`, and then publishes "
        "it and shows the live URL. Critically, the lead-capture form must be "
        "FLAT native `input`/`textarea`/`button{type:\"submit\"}` widgets with "
        "real field names (name, email, phone, message) — NEVER the `form` or "
        "`newsletter` widget, which nests an invalid `<form>` inside the site "
        "template's outer POST form and captures zero leads. Pricing uses "
        "`pricing-table` with `tiers`, CTAs are anchor links, and any animation "
        "stays CSS-only (Tier-0).\n"
        "If that skill is unavailable, fall back directly with the MCP tools: "
        "call `mcp__pocketpaw_pocket_specialist__create` to build the "
        "conversion-ordered landing spec (stamp `type=\"site\"` + "
        "`pattern=\"landing\"`, flat named lead inputs, `pricing-table` tiers, "
        "anchor CTAs), then `mcp__pocketpaw_sites_manager__publish` with the "
        "returned pocket_id.\n"
        "Either way: relay any publish error — never claim a phantom publish — and "
        "after it succeeds, SHOW the live `url` plus a link to /sites where the "
        "user manages their sites. Keep talking 'site' / 'page', never 'pocket'.\n"
        "</sites-procedure>"
    )


__all__ = ["build_preamble"]
