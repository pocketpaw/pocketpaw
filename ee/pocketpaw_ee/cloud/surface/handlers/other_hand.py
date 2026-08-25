# other_hand.py — /other-hand surface preamble (Otherhand v1).
#
# Created: 2026-08-25 (feat/other-hand-surface) — orients the chat agent when
# the user is on the Otherhand surface: a notebook page they handwrite on, which
# the agent then writes and DRAWS back onto. Not a chat. Turn-taking on one page.
#
# The preamble's whole job is to hand over two facts the agent cannot obtain any
# other way, and both arrive on ``SurfaceMeta``:
#
#   * ``snapshot_path`` — where the page image is. The agent cannot SEE the page
#     otherwise: attachments on this pipeline are text-extracted (an image upload
#     yields an empty stub), and the SDK is invoked with a plain ``prompt: str``,
#     so there are no content blocks and no vision call. What there IS is
#     ``Read``, which is in the agent's default SDK tool set and reads images
#     natively. So the page is written to disk by the snapshot endpoint and the
#     agent reads it off disk. That is the entire vision path.
#   * ``free_y`` — the y below which the page is empty. The one rule that makes
#     the surface usable rather than destructive: the agent must never write over
#     the user's own ink. The frontend re-checks this with a placement guard, so
#     this is guidance and not the enforcement; the agent aims, the app snaps.
#
# Read-only and idempotent by construction — the handler reads NOTHING. It has no
# upstream service, so its "fails to empty string on any error" invariant has a
# different shape than a service-backed handler's: the failure it must survive is
# a turn arriving with the hints MISSING (an older client, a snapshot POST that
# failed, a stray turn stamped other_hand from somewhere else). It returns a bare
# ``<surface kind="other_hand" />`` tag in that case rather than a preamble
# claiming a path that does not exist, and the broad try/except keeps even an
# unforeseen error inside the contract.
#
# The cache key is exact rather than a digest: the text is a pure function of the
# route, the snapshot path, and free_y, all named. It moves on EVERY turn in
# practice, because a fresh snapshot writes a new free_y whenever the user adds a
# stroke — which is correct, since a preamble pointing at last turn's empty line
# is a preamble that tells the agent to draw over the ink the user just laid down.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import meta_key

logger = logging.getLogger(__name__)

# The page's fixed logical canvas — A4 at 150dpi, portrait. FROZEN by the v1
# frontend/backend contract; the frontend scales to the device and the agent
# never sees device pixels. Duplicated in ``system_prompts.OTHER_HAND_SYSTEM_PROMPT``
# (where the agent is told the full op vocabulary); if one moves, move both.
PAGE_WIDTH = 1240
PAGE_HEIGHT = 1754

# The canonical route. ``_route_for`` derives "/other_hand" from the enum value
# for the registry's value-derived contract; the literal frontend route is
# "/other-hand" (hyphen), which is what the user is actually looking at and so
# what the preamble names.
ROUTE = "/other-hand"


def _clamp_free_y(raw: str | None) -> int | None:
    """Coerce the client's ``free_y`` hint to a usable page coordinate.

    Returns ``None`` when the hint is absent or not a number — the caller then
    renders the degraded preamble rather than inventing a line to draw below.
    A value outside the page is clamped rather than rejected: a nonsensical
    free_y is a frontend bug, but the honest response to "the page is full" is
    still to tell the agent the bottom margin, not to drop the surface.
    """
    if raw is None:
        return None
    try:
        value = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        logger.debug("other_hand_handler: unparseable free_y %r", raw)
        return None
    return max(0, min(value, PAGE_HEIGHT))


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the /other-hand surface preamble — the page, and where it is empty.

    Per the handler contract: read-only (nothing is read at all), workspace-scoped
    (vacuously — the handler touches no store; the snapshot path it echoes was
    built server-side from this workspace's jail root, so it cannot name another
    tenant's file), idempotent, and never raising.
    """
    try:
        route = meta.route_path or ROUTE
        snapshot_path = (meta.snapshot_path or "").strip()
        free_y = _clamp_free_y(meta.free_y)

        if not snapshot_path or free_y is None:
            # No page to look at. Say what the surface IS and stop — a preamble
            # that names a path the agent cannot read teaches it to report on a
            # page it never saw.
            logger.debug(
                "other_hand_handler: turn missing snapshot hints "
                "(path=%r free_y=%r); rendering degraded preamble",
                meta.snapshot_path,
                meta.free_y,
            )
            return SurfacePreamble(
                text=(
                    f'<surface kind="other_hand" route="{route}" />\n'
                    "The user is on their notebook page, but this turn carried no "
                    "page image. Answer in one short sentence and do not emit a "
                    "page-ops block — there is no page state to draw against.\n"
                ),
                cache_key=meta_key("other_hand", route, "no-snapshot"),
            )

        return SurfacePreamble(
            text=(
                f'<surface kind="other_hand" route="{route}" />\n'
                "You are writing on the user's notebook page with them. "
                "This is not a chat.\n"
                f"The page image is at: {snapshot_path}\n"
                "Read it to see what the user has written and drawn.\n"
                f"The page below y={free_y} is empty. "
                f"Put everything you add at y >= {free_y}.\n"
                "Reply with a short sentence, then ONE ```page-ops``` block.\n"
                "Never mention files, paths, coordinates, or tools to the user.\n"
            ),
            cache_key=meta_key("other_hand", route, snapshot_path, free_y),
        )
    except Exception:
        # No upstream to fail, so this catch is for the unforeseen only. The
        # contract still wants a string, and the surface tag is the most useful
        # string available — it keeps the agent oriented even with no page.
        logger.debug("other_hand_handler: preamble render failed", exc_info=True)
        return SurfacePreamble(text='<surface kind="other_hand" />', cache_key=None)


__all__ = ["ROUTE", "build_preamble"]
