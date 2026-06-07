# Calendar module — public surface.
# Created: 2026-05-19 (feat/calendar-module).
#
# Re-exports the router and public domain types. Beanie documents
# (_EventDoc, _CalendarDoc) stay private — never re-exported.

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
