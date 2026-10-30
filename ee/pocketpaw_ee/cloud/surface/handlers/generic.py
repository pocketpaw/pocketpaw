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
#
# Changes: 2026-10-01 (feat/rooms-read-tool) — ``_ROOMS_HINT``: the workspace's
# own chat rooms are PocketPaw rooms (list_rooms / read_room), not Slack. Asked
# to "catch me up on #general", the agent had assumed Slack.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import meta_key

_OPEN_SURFACE_HINT = (
    "<open-surface>If you have the open_surface tool, open an app surface when the "
    "user needs to SEE or CHOOSE something: /files to upload or pick files; to edit "
    "a video, /files first unless a clip is already known, then /studio/editor with "
    "the clip handoff params src, name, mime, kind (src is the file's /api/v1/uploads/"
    "<id> or /api/v1/media/<name> path); /chat to read a conversation. "
    "Do not open a surface for a question you can answer in text.</open-surface>"
)

_ROOMS_HINT = (
    "<rooms>If you have the list_rooms / read_room tools: this workspace's own chat "
    "rooms (channels like #general, groups, DMs) are PocketPaw rooms, so read them "
    "with those. Don't assume Slack unless the user names Slack.</rooms>"
)


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the generic-surface preamble."""
    route = meta.route_path or "?"
    return SurfacePreamble(
        text=(
            f'<surface kind="generic" route="{route}" />\n'
            "<surface-snapshot>(no specific surface context available — "
            "answer using the user's last message and ordinary chat "
            "tools)</surface-snapshot>\n" + _OPEN_SURFACE_HINT + "\n" + _ROOMS_HINT
        ),
        cache_key=meta_key("generic", route),
    )


__all__ = ["build_preamble"]
