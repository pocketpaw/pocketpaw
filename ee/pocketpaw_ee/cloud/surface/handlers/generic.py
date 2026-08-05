# generic.py — Fallback preamble for unknown surfaces.
#
# Created: 2026-05-24 — Catch-all for any surface kind we don't know
# yet (client shipped a new surface name before the backend handler
# shipped, or the client doesn't tag at all). The preamble is short on
# purpose — we don't want to fake live data the agent can't trust.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble`` keyed on ``meta.route_path``, the one input this handler
# reads. Deliberately faking no live data is what makes that exact: the text is
# a pure function of the route, so the key is the route. It still has to be
# there — a user moving between two unclassified routes changes the preamble,
# and the digest has to see it.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import meta_key


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the generic-surface preamble."""
    route = meta.route_path or "?"
    return SurfacePreamble(
        text=(
            f'<surface kind="generic" route="{route}" />\n'
            "<surface-snapshot>(no specific surface context available — "
            "answer using the user's last message and ordinary chat "
            "tools)</surface-snapshot>"
        ),
        cache_key=meta_key("generic", route),
    )


__all__ = ["build_preamble"]
