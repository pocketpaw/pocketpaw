# sidepanel.py — /sidepanel surface preamble.
#
# Created: 2026-05-24 — The side-panel Tauri window is a thinner chat
# surface. Like QuickAsk, no persistent state to share.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the sidepanel-surface preamble."""
    return (
        '<surface kind="sidepanel" route="/sidepanel" />\n'
        "<sidepanel-snapshot>(side-panel chat — no canvas state; "
        "answer concisely)</sidepanel-snapshot>"
    )


__all__ = ["build_preamble"]
