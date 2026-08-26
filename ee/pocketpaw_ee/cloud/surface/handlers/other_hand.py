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

import hashlib
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
    # The paper grows downward (2026-08-26), so free_y may exceed one sheet.
    # Clamp only to the growth ceiling the wire enforces (30 sheets).
    return max(0, min(value, PAGE_HEIGHT * 30))


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
        book_path = (meta.book_path or "").strip()
        mark_box = (meta.mark_box or "").strip()
        mark_image_path = (meta.mark_image_path or "").strip()
        mark_text = (meta.mark_text or "").strip()
        # The tutor stance for this turn, picked in the chat panel. "teach" is
        # the default and adds nothing — the system prompt's teaching playbook
        # already is that stance. The other two shift the register, not the
        # rules: quiz drives retrieval, explain suspends the hand-the-pen-back
        # habit for a student who just wants the thing stated.
        mode = (meta.mode or "").strip().lower()
        scene = (meta.scene or "").strip()

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

        # Book mode: a second, READ-ONLY image sits beside the notebook. The
        # agent reads it for what the user circled or underlined, and answers on
        # the NOTEBOOK. Saying "never draw on the book" is not enough on its own
        # — the ops coordinate space simply does not address the book, so the
        # instruction and the geometry agree.
        book_block = ""
        if book_path:
            # Two things make a mark legible to the agent, and it needs BOTH:
            # the colour convention (a thin dark line on a printed page reads as
            # part of the document) and the exact box (a sprawling hand-drawn
            # loop over dense text is ambiguous even to a human).
            if not mark_box:
                mark_block = "The reader has not marked anything yet.\n"
            else:
                parts = [
                    f"The reader's marks span the region {mark_box} "
                    "(x1,y1,x2,y2 in that image's coordinates).\n"
                ]
                # The text layer is EXACT — it is what the PDF itself says is
                # there, not what a vision pass thinks it read. When present it
                # is authoritative, and saying so stops the agent second-guessing
                # it against a downscaled raster.
                if mark_text:
                    parts.append(
                        "They marked this exact passage, taken from the "
                        f"document's own text:\n---\n{mark_text}\n---\n"
                        "That text is authoritative. Answer about IT.\n"
                    )
                if mark_image_path:
                    parts.append(
                        "A high-resolution image of just that region is at: "
                        f"{mark_image_path}\n"
                        "Read it when the passage is a figure, an equation, a "
                        "table, or a scan — anything the text above does not "
                        "capture.\n"
                    )
                if not mark_text and not mark_image_path:
                    parts.append(
                        "Read the text inside that region on the book page.\n"
                    )
                mark_block = "".join(parts)
            book_block = (
                # "source" not "book": it may be a PDF page OR an image the
                # user opened — a photo, a screenshot, a scan, a diagram. The
                # agent should describe what it actually sees rather than
                # calling a photograph a book page.
                f"The user has a SOURCE open beside the notebook — a document "
                f"page or an image — at: {book_path}\n"
                "Read it too. It is READ-ONLY.\n"
                "Everything drawn in RED on that page is the READER'S mark, "
                "not part of the document: a loop means 'this passage', a line "
                "under text means 'this line', a line through text means 'I do "
                "not follow this'.\n"
                f"{mark_block}"
                "Answer on the NOTEBOOK page. Every coordinate you emit "
                "addresses the notebook, never the book.\n"
            )

        mode_block = ""
        if mode == "quiz":
            mode_block = (
                "MODE: quiz. Do not explain unprompted this turn — write 2-3 "
                "retrieval questions on the page about what the student has "
                "been working on, hardest last, each with room to answer. "
                "Withhold the answers until they write theirs.\n"
            )
        elif mode == "explain":
            mode_block = (
                "MODE: explain. The student wants it stated plainly this turn: "
                "give the full, direct explanation with a clear diagram. Skip "
                "the check-question habit; do not quiz.\n"
            )

        return SurfacePreamble(
            text=(
                f'<surface kind="other_hand" route="{route}" />\n'
                "You are writing on the user's notebook page with them. "
                "This is not a chat.\n"
                f"The page image is at: {snapshot_path}\n"
                "Read it to see what the user has written and drawn. The image "
                "is EXACTLY the 1240x1754 coordinate space: a thing at pixel "
                "(x,y) in it is at coordinate (x,y) on the page.\n"
                f"{book_block}"
                f"The page below y={free_y} is empty. "
                f"Put everything you add at y >= {free_y}.\n"
                + (
                    "What is already on the page, with EXACT coordinates "
                    "(measure from these, not from memory — your earlier ops "
                    "may have been shifted to avoid the user's ink):\n"
                    f"{scene}\n"
                    "texts: content with its anchor. shapes/user: bounding "
                    "boxes [x1,y1,x2,y2]. Anchor every annotation arrow to "
                    "one of these.\n"
                    if scene
                    else ""
                )
                + f"{mode_block}"
                "Reply with a short sentence, then ONE ```page-ops``` block.\n"
                "Never mention files, paths, coordinates, or tools to the user.\n"
            ),
            cache_key=meta_key(
                "other_hand",
                route,
                snapshot_path,
                free_y,
                book_path or "no-book",
                mark_box or "no-mark",
                mark_image_path or "no-mark-image",
                mark_text[:120] or "no-mark-text",
                mode or "teach",
                hashlib.md5(scene.encode()).hexdigest()[:8] if scene else "no-scene",
            ),
        )
    except Exception:
        # No upstream to fail, so this catch is for the unforeseen only. The
        # contract still wants a string, and the surface tag is the most useful
        # string available — it keeps the agent oriented even with no page.
        logger.debug("other_hand_handler: preamble render failed", exc_info=True)
        return SurfacePreamble(text='<surface kind="other_hand" />', cache_key=None)


__all__ = ["ROUTE", "build_preamble"]
