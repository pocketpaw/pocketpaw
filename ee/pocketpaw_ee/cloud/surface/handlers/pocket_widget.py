# pocket_widget.py — Pocket-with-widget-focus-modal-open preamble.
#
# Created: 2026-05-24 — Same context as the pocket handler plus a
# pointer to the focused widget. We deliberately do NOT dump the full
# spec subtree — only its name / type and the focus_node_id — to keep
# the preamble within the 1500-char budget. The agent already has
# tools to read the spec on demand.

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
    return truncate_preamble(f"{base}\n{focus_block}")


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
