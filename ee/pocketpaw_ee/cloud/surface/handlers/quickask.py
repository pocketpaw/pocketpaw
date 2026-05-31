# quickask.py — QuickAsk overlay surface preamble.
#
# Created: 2026-05-24 — Minimal preamble — the QuickAsk window is the
# "Spotlight for your AI" launcher, not a surface with persistent
# state. Tell the agent which surface it's on; nothing else to share.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the quickask-surface preamble."""
    return (
        '<surface kind="quickask" route="/quickask" />\n'
        "<quickask-snapshot>(QuickAsk overlay — no persistent surface "
        "state; answer concisely)</quickask-snapshot>"
    )


__all__ = ["build_preamble"]
