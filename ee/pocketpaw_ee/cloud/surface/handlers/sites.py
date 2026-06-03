# sites.py — /sites surface preamble.
#
# Created: 2026-06-02 — Orients the chat agent when the user is on the /sites
# surface (the Paw Sites gallery + describe-to-create rail). Without it the
# surface fell back to GENERIC and the agent built + talked "pocket" instead of
# a publishable website (the operator-reported "agent builds pockets not sites"
# drift). Static orientation — no live data to fake.
#
# Updated: 2026-06-03 — Folded the create→publish PROCEDURE into the preamble.
# The /sites create flow used to fire a `/pocketpaw-create-site` slash command
# to invoke the create-site skill, but the chat agent runs the Claude Agent SDK
# with `setting_sources=[]` (persona isolation) which disables skill discovery,
# so the slash command was a silent no-op. The frontend now sends the user's
# description as PLAIN TEXT, so the two-step the skill used to carry (create the
# source pocket via the pocket-specialist MCP tool, then publish it via the
# sites-manager MCP tool, then show the live URL) has to live in this always-on
# preamble instead. We instruct the agent to call the MCP tools DIRECTLY — not
# to invoke a Skill, since skills can't load under setting_sources=[].

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
        "PUBLISH a site. Do it directly via the MCP tools below — do NOT invoke a "
        "skill or slash command (they don't load here):\n"
        "1. CREATE the source spec by calling the "
        "`mcp__pocketpaw_pocket_specialist__create` tool with a marketing/landing "
        "layout: a hero (page-header), a few value/content sections, and a "
        "contact / lead-capture form (form-layout) whose inputs have CLEAR field "
        "names — e.g. full_name, email, phone, message — so the published site "
        "captures leads out of the box.\n"
        "2. PUBLISH it as a live site by calling the "
        "`mcp__pocketpaw_sites_manager__publish` tool with the new pocket_id "
        "returned in step 1. If it returns an error, relay it — never claim a "
        "phantom publish.\n"
        "3. SHOW the user the live `url` from the publish response plus a link to "
        "/sites where they manage their sites. Keep talking 'site' / 'page'.\n"
        "</sites-procedure>"
    )


__all__ = ["build_preamble"]
