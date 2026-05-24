# settings.py — /settings surface preamble.
#
# Created: 2026-05-24 — Minimal preamble. The settings surface is a
# configuration tool; we don't want the agent leaking config values into
# chat. Tell it the surface and stop there.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the settings-surface preamble."""
    return (
        '<surface kind="settings" route="/settings" />\n'
        "<settings-snapshot>(user is configuring the workspace — "
        "answer as a configuration-aware assistant. No live data "
        "snapshot for this surface.)</settings-snapshot>"
    )


__all__ = ["build_preamble"]
