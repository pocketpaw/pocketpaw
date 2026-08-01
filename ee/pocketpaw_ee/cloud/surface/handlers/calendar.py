# calendar.py — /calendar surface preamble.
#
# Updated: 2026-05-24 (feat/calendar-entity-surface, #1218) — restored
# the third rendering state so "Composio off" and "Composio on but no
# events" no longer collapse to the same hint. Three distinct branches
# now drive the snapshot block:
#
#   1. Events present — render one line per event ("- 10:30 AM ·
#      Sync with Sarah") inside the snapshot block.
#   2. Composio enabled but no upcoming events — render
#      ``<calendar-snapshot>(no upcoming events)</calendar-snapshot>``
#      so the agent knows the integration works and the calendar is
#      genuinely empty.
#   3. Composio disabled OR the service raised — render the static
#      hint pointing at GOOGLECALENDAR_LIST_EVENTS so the agent still
#      discovers the action and the user can be guided to connect.
#
# We probe ``composio_service.is_enabled()`` from the handler (lazy
# import, same as ``list_upcoming``) to split branches 2 and 3 without
# changing the service signature — the brief calls this out as the
# minimum-blast-radius split.
#
# Surface tag is always emitted so the agent always knows which route
# the user is on, regardless of which branch above ran.
#
# Changes: 2026-08-02 (PA-2, feat/prompt-assembler-seam) — returns a
# ``SurfacePreamble``. Live mutable state (the upcoming-events feed) with no
# revision to key on, so the listed-events branch keys on a digest of the
# rendered rows; the three branches that render a fixed block key on WHICH
# block, which is exact. Worth noting for the churn budget: the rendered rows
# carry event times, so a schedule that changes between turns moves the digest
# — that is the feed changing, not the key being noisy.

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta, SurfacePreamble
from pocketpaw_ee.cloud.surface.handlers._helpers import (
    content_key,
    meta_key,
    truncate_preamble,
)

logger = logging.getLogger(__name__)

# Cap on how many events we list inline. 10 keeps the preamble well
# under the per-handler 1500-char soft cap even on dense calendars.
LIST_LIMIT = 10

# Static block emitted when Composio isn't configured or the service
# raised. The hint surfaces the action name so the agent still
# discovers what tool to reach for when the user asks.
_COMPOSIO_HINT = (
    "<calendar-snapshot>(no live event feed wired — use "
    "GOOGLECALENDAR_LIST_EVENTS via Composio if available)</calendar-snapshot>"
)

# Distinct block for the "Composio is on, calendar is connected,
# nothing on the schedule" case. Keeping this separate from the hint
# lets the agent tell "no integration" apart from "genuinely empty".
_EMPTY_SNAPSHOT = "<calendar-snapshot>(no upcoming events)</calendar-snapshot>"

_SURFACE_TAG = '<surface kind="calendar" route="/calendar" />'


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> SurfacePreamble:
    """Render the calendar-surface preamble.

    Always returns a usable preamble — never raises. The chat path drops
    surface failures silently, but we belt-and-braces it here so a
    quick local error doesn't cost a network round-trip on every send.

    The cache key follows what was read. The three no-event branches read a
    live feed but render one of two STATIC blocks, so they key on which block
    (exact — the text cannot vary within a branch); the listed-events branch
    keys on a digest of the rendered rows, which moves as the schedule does.
    """
    try:
        from pocketpaw_ee.cloud.calendar.service import list_upcoming

        events = await list_upcoming(workspace_id, user_id, limit=LIST_LIMIT)
    except Exception:
        logger.debug("calendar_handler: list_upcoming raised", exc_info=True)
        return SurfacePreamble(
            text=truncate_preamble(f"{_SURFACE_TAG}\n{_COMPOSIO_HINT}"),
            cache_key=meta_key("calendar", "no-feed"),
        )

    if not events:
        # No events AND no error. Two sub-states the agent needs to tell
        # apart: Composio is on (calendar genuinely empty) vs Composio
        # is off (no integration at all). Probe is_enabled() to pick.
        # Lazy import mirrors the list_upcoming pattern above — keeps
        # the cold-path cheap and the test monkeypatch surface clean.
        try:
            from pocketpaw_ee.cloud.composio import service as composio_service

            composio_enabled = composio_service.is_enabled()
        except Exception:
            logger.debug("calendar_handler: is_enabled probe failed", exc_info=True)
            composio_enabled = False
        if composio_enabled:
            return SurfacePreamble(
                text=truncate_preamble(f"{_SURFACE_TAG}\n{_EMPTY_SNAPSHOT}"),
                cache_key=meta_key("calendar", "empty"),
            )
        return SurfacePreamble(
            text=truncate_preamble(f"{_SURFACE_TAG}\n{_COMPOSIO_HINT}"),
            cache_key=meta_key("calendar", "no-feed"),
        )

    rows = [_format_event_line(ev) for ev in events[:LIST_LIMIT]]
    snapshot = (
        f'<calendar-snapshot count="{len(events)}">\n' + "\n".join(rows) + "\n</calendar-snapshot>"
    )
    text = truncate_preamble(f"{_SURFACE_TAG}\n{snapshot}")
    return SurfacePreamble(text=text, cache_key=content_key("calendar", text))


# ---------------------------------------------------------------------------
# Rendering helpers — private to this handler.
# ---------------------------------------------------------------------------


def _format_event_line(event: dict[str, Any]) -> str:
    """Format one event for the snapshot block.

    Target shape: ``- 10:30 AM · Sync with Sarah``. Falls back to the
    raw ISO string when ``start`` can't be parsed — better to show
    something than nothing. Title is collapsed to the first line if
    the upstream stores a multi-line summary.
    """
    title = str(event.get("title") or "(no title)").strip().splitlines()[0]
    start_raw = str(event.get("start") or "")
    when = _format_start_time(start_raw)
    if when:
        return f"- {when} · {title}"
    return f"- {title}"


def _format_start_time(iso: str) -> str:
    """Render a start timestamp into a human-friendly time-of-day.

    Returns an empty string when ``iso`` is empty or can't be parsed,
    or the date when the event is all-day (date-only ISO). Hour formatting
    is locale-naive but stable: ``10:30 AM``, ``2:00 PM``.
    """
    if not iso:
        return ""
    # All-day events arrive as ``YYYY-MM-DD`` (no T). Surface those as the
    # date itself — there's no time-of-day to render.
    if "T" not in iso:
        return iso
    try:
        # ``fromisoformat`` accepts both naive and aware ISO strings
        # (the latter as of 3.11 with the ``Z`` suffix normalization
        # below). Strip the trailing Z to keep older Pythons happy.
        cleaned = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    except ValueError:
        return iso
    return dt.strftime("%I:%M %p").lstrip("0")


__all__ = ["build_preamble"]
