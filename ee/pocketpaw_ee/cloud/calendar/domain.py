# domain.py — Cloud calendar entity value objects.
#
# Created: 2026-05-24 (feat/calendar-entity-surface, #1214) — frozen
# dataclass with workspace_id required at construction. Mirrors the
# ee/cloud rule: domain enforces multi-tenancy at construction time so
# the workspace tag is impossible to forget. Field order keeps
# workspace_id first after id, so any positional construction lands
# tenancy in front; missing workspace_id is a TypeError, not silent.
#
# ISO-string start/end (not datetime). The data flows in from Composio
# as JSON strings (Google Calendar returns RFC 3339 timestamps) and
# flows back out to the surface preamble as strings — there's no
# date math in the read path, so parsing the strings would be pure
# overhead. Domain stays as plain pass-through value objects.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CalendarEvent:
    """A single upcoming calendar event scoped to a workspace.

    Tenancy is enforced at construction — ``workspace_id`` is required
    positionally with no default. Constructing a ``CalendarEvent``
    without one is a TypeError. Same rule as the rest of ``ee/cloud``.

    Fields:
      * ``id`` — the upstream provider's event id (Google Calendar
        event id when sourced from Composio).
      * ``workspace_id`` — owning workspace. Tagged at construction so
        downstream code can fan out events across workspaces without
        accidentally bleeding one tenant into another.
      * ``title`` — event title (Google Calendar ``summary``). Defaults
        only to a placeholder when the upstream payload omits it.
      * ``start`` / ``end`` — ISO 8601 strings. RFC 3339 ``dateTime``
        when the event has a time component; date-only ``YYYY-MM-DD``
        when the event is all-day. Empty string when missing upstream.
      * ``attendees`` — list of email strings. Optional; defaults to
        an empty list. Each entry is the raw email — no normalization
        is applied at this layer (downstream services may dedupe).
      * ``source`` — upstream system slug (e.g. ``"google"``, ``"ical"``).
        Distinct from "the connector" so future providers can ship
        without renaming the field.
    """

    id: str
    workspace_id: str
    title: str
    start: str
    end: str
    source: str
    attendees: list[str] = field(default_factory=list)


__all__ = ["CalendarEvent"]
