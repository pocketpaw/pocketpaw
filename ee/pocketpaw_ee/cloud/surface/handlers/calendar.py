# calendar.py — /calendar surface preamble.
#
# Created: 2026-05-24 — Placeholder while the calendar entity is still
# in flight. Emits the surface tag so the agent knows the route and
# notes that no live snapshot is available yet — the agent then knows
# to use Composio's GOOGLECALENDAR_LIST_EVENTS rather than guess.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the calendar-surface preamble."""
    return (
        '<surface kind="calendar" route="/calendar" />\n'
        "<calendar-snapshot>(no live event feed wired — use "
        "GOOGLECALENDAR_LIST_EVENTS via Composio if available)</calendar-snapshot>"
    )


__all__ = ["build_preamble"]
