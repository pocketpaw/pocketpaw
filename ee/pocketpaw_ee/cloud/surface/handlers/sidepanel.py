# sidepanel.py — /sidepanel surface preamble.
#
# Created: 2026-05-24 — The side-panel Tauri window is a thinner chat
# surface. Like QuickAsk, no persistent state to share.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble`` with a constant key. No state to share means no state to
# key on: the text is a literal and cannot differ between two turns.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import meta_key


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the sidepanel-surface preamble."""
    return SurfacePreamble(
        text=(
            '<surface kind="sidepanel" route="/sidepanel" />\n'
            "<sidepanel-snapshot>(side-panel chat — no canvas state; "
            "answer concisely)</sidepanel-snapshot>"
        ),
        cache_key=meta_key("sidepanel"),
    )


__all__ = ["build_preamble"]
