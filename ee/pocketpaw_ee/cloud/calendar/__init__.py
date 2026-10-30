# __init__.py — Public surface for the cloud calendar entity.
#
# Created: 2026-05-24 (feat/calendar-entity-surface, #1214) — the
# cloud-side calendar entity is a thin read-only adapter that surfaces
# upcoming events from external providers (today: Composio →
# Google Calendar) into the surface preamble. Distinct from the native
# scheduling subsystem under ``ee/pocketpaw_ee/calendar/`` — that one
# owns events, attendees, recurrence and free/busy as first-class
# entities; this one is a thin entity that the surface handler reads.
#
# Re-exports the public domain value object and the read service. No
# router — the surface handler is the only consumer for now.

from __future__ import annotations

from pocketpaw_ee.cloud.calendar.domain import CalendarEvent
from pocketpaw_ee.cloud.calendar.dto import CalendarEventResponse
from pocketpaw_ee.cloud.calendar.service import list_upcoming

__all__ = ["CalendarEvent", "CalendarEventResponse", "list_upcoming"]
