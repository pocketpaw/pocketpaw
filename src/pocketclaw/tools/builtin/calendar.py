# Google Calendar tools — list events, create events, meeting prep.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketclaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


class CalendarListTool(BaseTool):
    """List upcoming Google Calendar events."""

    @property
    def name(self) -> str:
        return "calendar_list"

    @property
    def description(self) -> str:
        return (
            "List upcoming events from Google Calendar. "
            "Can filter by date range. Returns event titles, times, locations, and attendees."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "days_ahead": {
                    "type": "integer",
                    "description": "Number of days to look ahead (default: 1)",
                    "default": 1,
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of events (default: 10)",
                    "default": 10,
                },
            },
            "required": [],
        }

    async def execute(self, days_ahead: int = 1, max_results: int = 10) -> str:
        from pocketclaw.integrations.gcalendar import CalendarClient

        try:
            client = CalendarClient()
            now = datetime.now(UTC)
            events = await client.list_events(
                time_min=now,
                time_max=now + timedelta(days=max(days_ahead, 1)),
                max_results=min(max_results, 50),
            )

            if not events:
                return f"No events in the next {days_ahead} day(s)."

            lines = [f"Found {len(events)} event(s):\n"]
            for i, ev in enumerate(events, 1):
                loc = f" @ {ev['location']}" if ev["location"] else ""
                attendees = ""
                if ev["attendees"]:
                    attendees = f"\n   Attendees: {', '.join(ev['attendees'][:5])}"
                lines.append(
                    f"{i}. **{ev['summary']}**\n   {ev['start']} → {ev['end']}{loc}{attendees}\n"
                )
            return "\n".join(lines)

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Calendar list failed: {e}")


class CalendarCreateTool(BaseTool):
    """Create a Google Calendar event."""

    @property
    def name(self) -> str:
        return "calendar_create"

    @property
    def description(self) -> str:
        return "Create a new event on Google Calendar with title, time, location, and attendees."

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Event title",
                },
                "start": {
                    "type": "string",
                    "description": "Start time in ISO 8601 (e.g., 2026-02-08T10:00:00-08:00)",
                },
                "end": {
                    "type": "string",
                    "description": "End time in ISO 8601 (e.g., 2026-02-08T11:00:00-08:00)",
                },
                "description": {
                    "type": "string",
                    "description": "Event description (optional)",
                },
                "location": {
                    "type": "string",
                    "description": "Event location (optional)",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of attendee emails (optional)",
                },
            },
            "required": ["summary", "start", "end"],
        }

    async def execute(
        self,
        summary: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
        attendees: list[str] | None = None,
    ) -> str:
        from pocketclaw.integrations.gcalendar import CalendarClient

        try:
            client = CalendarClient()
            result = await client.create_event(
                summary=summary,
                start=start,
                end=end,
                description=description,
                location=location,
                attendees=attendees,
            )
            return f"Event created: **{result['summary']}**\nLink: {result['htmlLink']}"

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Failed to create event: {e}")


class CalendarPrepTool(BaseTool):
    """Prepare a briefing for the next meeting."""

    @property
    def name(self) -> str:
        return "calendar_prep"

    @property
    def description(self) -> str:
        return (
            "Get a briefing for your next upcoming meeting. "
            "Shows event details, attendee list, and any relevant context."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    async def execute(self) -> str:
        from pocketclaw.integrations.gcalendar import CalendarClient

        try:
            client = CalendarClient()
            now = datetime.now(UTC)
            events = await client.list_events(
                time_min=now,
                time_max=now + timedelta(hours=24),
                max_results=1,
            )

            if not events:
                return "No upcoming meetings in the next 24 hours."

            ev = events[0]
            lines = [
                "**Next Meeting Briefing**\n",
                f"Title: {ev['summary']}",
                f"Time: {ev['start']} → {ev['end']}",
            ]
            if ev["location"]:
                lines.append(f"Location: {ev['location']}")
            if ev["description"]:
                lines.append(f"Description: {ev['description'][:500]}")
            if ev["attendees"]:
                lines.append(f"Attendees ({len(ev['attendees'])}):")
                for a in ev["attendees"][:10]:
                    lines.append(f"  - {a}")

            return "\n".join(lines)

        except RuntimeError as e:
            return self._error(str(e))
        except Exception as e:
            return self._error(f"Meeting prep failed: {e}")
