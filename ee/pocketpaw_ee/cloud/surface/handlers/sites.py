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
        "dashboard. Design a real marketing landing page: a hero, a few content "
        "sections, and a lead-capture form whose inputs have clear field names. "
        "Talk about it as a 'site' or 'page' — never a 'pocket'. The pocket is "
        "only the source spec; it auto-publishes to a live URL, so favor clean "
        "marketing copy, real sections, and a working contact / sign-up form over "
        "generic dashboard widgets.\n"
        "</sites-orientation>\n"
        "<sites-procedure>\n"
        "Treat the user's message on this surface as a request to BUILD AND "
        "PUBLISH a site. PREFER the `pocketpaw-create-site` skill — invoke it by "
        "intent (no slash command needed). It carries the whole flow: build the "
        "source pocket as a marketing/landing page — a hero (page-header), a few "
        "value/content sections, and a contact / lead-capture form (form-layout) "
        "whose inputs have CLEAR field names like full_name, email, phone, "
        "message so the published site captures leads out of the box — then "
        "publish it and show the live URL.\n"
        "If the skill is unavailable, fall back to the same two steps directly "
        "with the MCP tools: call `mcp__pocketpaw_pocket_specialist__create` to "
        "build the landing-page spec, then `mcp__pocketpaw_sites_manager__publish` "
        "with the returned pocket_id.\n"
        "Either way: relay any publish error — never claim a phantom publish — and "
        "after it succeeds, SHOW the live `url` plus a link to /sites where the "
        "user manages their sites. Keep talking 'site' / 'page', never 'pocket'.\n"
        "</sites-procedure>"
    )


__all__ = ["build_preamble"]
