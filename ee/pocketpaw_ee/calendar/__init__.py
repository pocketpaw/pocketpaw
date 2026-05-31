# Calendar module — public surface.
# Created: 2026-05-19 (feat/calendar-module).
# Updated: 2026-05-24 (feat/calendar-entity-surface, #1218) — added the
# sibling-module pointer to ``pocketpaw_ee.cloud.calendar`` so future
# contributors looking for "the calendar code" find both modules.
#
# Re-exports the router and public domain types. Beanie documents
# (_EventDoc, _CalendarDoc) stay private — never re-exported.
#
# For the surface-preamble read-adapter that proxies upcoming events
# into the chat agent's per-turn context, see
# ``pocketpaw_ee.cloud.calendar`` (separate module — different role,
# different code).

from pocketpaw_ee.calendar.domain import (
    Attendee,
    AttendeeResponse,
    Calendar,
    CalendarVisibility,
    ConflictSeverity,
    Event,
    FreeBusy,
    Recurrence,
)
from pocketpaw_ee.calendar.router import router

__all__ = [
    "Attendee",
    "AttendeeResponse",
    "Calendar",
    "CalendarVisibility",
    "ConflictSeverity",
    "Event",
    "FreeBusy",
    "Recurrence",
    "router",
]
