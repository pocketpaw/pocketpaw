# pocket_widget.py — Pocket-with-widget-focus-modal-open preamble.
#
# Updated: 2026-05-24 — Added workspace_id tenancy guard. The base
# preamble already delegates to ``pocket_handler.build_preamble`` which
# now rejects cross-workspace pocket_ids — but the focus block (widget /
# node pointer the client supplied) was still rendered unconditionally.
# A client could stamp a pocket from workspace B + widget/node ids from
# B in a chat from A and, even after the base unavailable fallback,
# those id pointers would still appear in the preamble. We now suppress
# the focus block whenever the pocket fetch would be cross-workspace so
# the entire B-context is dropped, not just the snapshot.
#
# Original: Same context as the pocket handler plus a pointer to the
# focused widget. We deliberately do NOT dump the full spec subtree —
# only its name / type and the focus_node_id — to keep the preamble
# within the 1500-char budget. The agent already has tools to read the
# spec on demand.

from __future__ import annotations

import logging

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta
from pocketpaw_ee.cloud.surface.handlers import pocket as pocket_handler
from pocketpaw_ee.cloud.surface.handlers._helpers import truncate_preamble

logger = logging.getLogger(__name__)


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Pocket preamble plus a one-line focus marker for the open widget."""
    base = await pocket_handler.build_preamble(workspace_id, user_id, meta)
    focus_block = _focus_block(meta)
    if not focus_block:
        return base
    # Tenancy guard: the base preamble already rejects cross-workspace
    # pockets, but the focus pointers came straight from the client. If
    # the pocket fetch was cross-workspace, drop the focus block too so
    # no workspace-B identifiers leak into a workspace-A chat context.
    if meta.pocket_id and not await _pocket_in_workspace(meta.pocket_id, user_id, workspace_id):
        return base
    return truncate_preamble(f"{base}\n{focus_block}")


async def _pocket_in_workspace(pocket_id: str, user_id: str, workspace_id: str) -> bool:
    """True iff ``pocket_id`` resolves to a pocket in ``workspace_id``.

    Mirrors the guard in ``pocket._load_pocket``. We deliberately repeat
    the check here rather than threading a second return value out of
    the base preamble so the two handlers stay independently auditable.
    """
    try:
        from pocketpaw_ee.cloud.pockets import service as pockets_service

        pocket = await pockets_service.get(pocket_id, user_id)
    except Exception:
        logger.debug(
            "pocket_widget_handler: workspace lookup for %s failed",
            pocket_id,
            exc_info=True,
        )
        return False
    if pocket.get("workspace") != workspace_id:
        logger.warning(
            "pocket_widget_handler: workspace mismatch for pocket %s "
            "(chat=%s, pocket=%s); dropping focus block",
            pocket_id,
            workspace_id,
            pocket.get("workspace"),
        )
        return False
    return True


def _focus_block(meta: SurfaceMeta) -> str:
    """Render the ``<widget-focus>`` tag if either id is present.

    We render whatever we have — widget id alone, node id alone, or
    both — so the agent knows what the user is looking at without
    needing to chase the modal's open state.
    """
    if not meta.widget_id and not meta.focus_node_id:
        return ""
    bits = []
    if meta.widget_id:
        bits.append(f'widget_id="{meta.widget_id}"')
    if meta.focus_node_id:
        bits.append(f'focus_node_id="{meta.focus_node_id}"')
    return f"<widget-focus {' '.join(bits)} />"


__all__ = ["build_preamble"]
