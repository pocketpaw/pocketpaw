# generic.py — Fallback preamble for unknown surfaces.
#
# Created: 2026-05-24 — Catch-all for any surface kind we don't know
# yet (client shipped a new surface name before the backend handler
# shipped, or the client doesn't tag at all). The preamble is short on
# purpose — we don't want to fake live data the agent can't trust.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the generic-surface preamble."""
    route = meta.route_path or "?"
    return (
        f'<surface kind="generic" route="{route}" />\n'
        "<surface-snapshot>(no specific surface context available — "
        "answer using the user's last message and ordinary chat "
        "tools)</surface-snapshot>"
    )


__all__ = ["build_preamble"]
