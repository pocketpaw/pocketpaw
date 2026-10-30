# quickask.py — QuickAsk overlay surface preamble.
#
# Created: 2026-05-24 — Minimal preamble — the QuickAsk window is the
# "Spotlight for your AI" launcher, not a surface with persistent
# state. Tell the agent which surface it's on; nothing else to share.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble``. This handler reads NOTHING — no ``meta``, no I/O, the
# text is a literal — so a constant key is not a shortcut here, it is the exact
# answer: this preamble cannot differ between two turns.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import meta_key


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the quickask-surface preamble."""
    return SurfacePreamble(
        text=(
            '<surface kind="quickask" route="/quickask" />\n'
            "<quickask-snapshot>(QuickAsk overlay — no persistent surface "
            "state; answer concisely)</quickask-snapshot>"
        ),
        cache_key=meta_key("quickask"),
    )


__all__ = ["build_preamble"]
