# settings.py — /settings surface preamble.
#
# Created: 2026-05-24 — Minimal preamble. The settings surface is a
# configuration tool; we don't want the agent leaking config values into
# chat. Tell it the surface and stop there.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble`` with a constant key. Deliberately reading no live config
# is the whole design of this handler, so there is nothing mutable for the key
# to track: the text is a literal and cannot differ between two turns.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import meta_key


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the settings-surface preamble."""
    return SurfacePreamble(
        text=(
            '<surface kind="settings" route="/settings" />\n'
            "<settings-snapshot>(user is configuring the workspace — "
            "answer as a configuration-aware assistant. No live data "
            "snapshot for this surface.)</settings-snapshot>"
        ),
        cache_key=meta_key("settings"),
    )


__all__ = ["build_preamble"]
