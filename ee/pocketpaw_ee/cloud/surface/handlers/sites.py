# sites.py — /sites surface preamble.
#
# Created: 2026-06-02 — Orients the chat agent when the user is on the /sites
# surface (the Paw Sites gallery + describe-to-create rail). Without it the
# surface fell back to GENERIC and the agent built + talked "pocket" instead of
# a publishable website (the operator-reported "agent builds pockets not sites"
# drift). Static orientation — no live data to fake.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the /sites surface preamble (static orientation, no live data)."""
    route = meta.route_path or "/sites"
    return (
        f'<surface kind="sites" route="{route}" />\n'
        "<sites-orientation>\n"
        "The user is on the SITES surface, building a publishable WEBSITE that "
        "deploys as a standalone static page on the edge — not an in-app pocket "
        "dashboard. Design a real marketing landing page: a hero, a few content "
        "sections, and a lead-capture form whose inputs have clear field names. "
        "Talk about it as a 'site' or 'page' — never a 'pocket'. The pocket you "
        "create is only the source spec; it will be auto-published to a live URL, "
        "so favor clean marketing copy, real sections, and a working contact / "
        "sign-up form over generic dashboard widgets.\n"
        "</sites-orientation>"
    )


__all__ = ["build_preamble"]
