# GoogleCalendarConnector — native adapter wrapping CalendarClient.
# Created: 2026-05-03 — Phase 1 PR-4 (follows the GmailConnector
# pattern). Wraps the existing CalendarClient at
# src/pocketpaw/clients/gcalendar.py — that client owns OAuth +
# RFC 3339 datetime serialization.
#
# Action surface mirrors the 3 hand-written tools in
# src/pocketpaw/tools/builtin/calendar.py + adds calendar_summary
# for the home widget aggregator.

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw.connectors.protocol import (
    ActionResult,
    ActionSchema,
    ConnectionResult,
    ConnectorHealth,
    ConnectorScope,
    ConnectorStatus,
    ExecutionMode,
    SyncResult,
    TrustLevel,
    WidgetRecipe,
)

logger = logging.getLogger(__name__)


class GoogleCalendarConnector:
    """Native Google Calendar connector implementing ConnectorProtocol."""

    @property
    def name(self) -> str:
        return "gcalendar"

    @property
    def display_name(self) -> str:
        return "Google Calendar"

    @property
    def type(self) -> str:
        return "communication"

    @property
    def icon(self) -> str:
        return "calendar"

    def __init__(self) -> None:
        self._connected = False

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        try:
            from pocketpaw.clients.gcalendar import CalendarClient

            client = CalendarClient()
            await client._get_token()  # noqa: SLF001
            self._connected = True
            return ConnectionResult(
                success=True,
                connector_name=self.name,
                status=ConnectorStatus.CONNECTED,
                message="Calendar connected",
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=str(exc),
            )

    async def disconnect(self, pocket_id: str) -> bool:
        self._connected = False
        return True

    async def actions(self) -> list[ActionSchema]:
        return [
            ActionSchema(
                name="calendar_list",
                description=(
                    "List upcoming calendar events. Defaults to the next 24 hours; "
                    "set days_ahead to look further."
                ),
                method="GET",
                parameters={
                    "days_ahead": {
                        "type": "integer",
                        "description": "Days to look ahead (default 1)",
                        "default": 1,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max events to return (default 10, capped at 50)",
                        "default": 10,
                    },
                },
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="calendar_create",
                description=(
                    "Create a new event on Google Calendar with title, time, "
                    "location, and attendees."
                ),
                method="POST",
                parameters={
                    "summary": {"type": "string", "description": "Event title"},
                    "start": {
                        "type": "string",
                        "description": "Start time in ISO 8601",
                    },
                    "end": {"type": "string", "description": "End time in ISO 8601"},
                    "description": {"type": "string", "description": "Event description"},
                    "location": {"type": "string", "description": "Event location"},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Attendee email addresses",
                    },
                },
                trust_level=TrustLevel.CONFIRM,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="calendar_prep",
                description=(
                    "Briefing for the next upcoming meeting — event details, "
                    "attendee list, location."
                ),
                method="GET",
                parameters={},
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
            ActionSchema(
                name="calendar_summary",
                description=(
                    "Aggregate calendar stats — today's count, this week's count, "
                    "next meeting time. Backs the Calendar Today widget."
                ),
                method="GET",
                parameters={},
                trust_level=TrustLevel.AUTO,
                execution_mode=ExecutionMode.CLOUD,
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        from pocketpaw.clients.gcalendar import CalendarClient

        try:
            client = CalendarClient()
            now = datetime.now(UTC)

            if action == "calendar_list":
                days_ahead = max(int(params.get("days_ahead", 1)), 1)
                max_results = min(int(params.get("max_results", 10)), 50)
                events = await client.list_events(
                    time_min=now,
                    time_max=now + timedelta(days=days_ahead),
                    max_results=max_results,
                )
                return ActionResult(success=True, data=events, records_affected=len(events))
            if action == "calendar_create":
                data = await client.create_event(
                    summary=params["summary"],
                    start=params["start"],
                    end=params["end"],
                    description=params.get("description", ""),
                    location=params.get("location", ""),
                    attendees=params.get("attendees"),
                )
                return ActionResult(success=True, data=data, records_affected=1)
            if action == "calendar_prep":
                events = await client.list_events(
                    time_min=now,
                    time_max=now + timedelta(days=1),
                    max_results=1,
                )
                if not events:
                    return ActionResult(
                        success=True,
                        data={"message": "No upcoming meetings in the next 24 hours."},
                    )
                return ActionResult(success=True, data=events[0], records_affected=1)
            if action == "calendar_summary":
                today_events = await client.list_events(
                    time_min=now,
                    time_max=now + timedelta(days=1),
                    max_results=50,
                )
                week_events = await client.list_events(
                    time_min=now,
                    time_max=now + timedelta(days=7),
                    max_results=50,
                )
                next_summary = today_events[0]["summary"] if today_events else "—"
                return ActionResult(
                    success=True,
                    data={
                        "today": len(today_events),
                        "this_week": len(week_events),
                        "next": next_summary,
                    },
                )
            return ActionResult(success=False, error=f"Unknown action: {action}")
        except RuntimeError as exc:
            return ActionResult(success=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return ActionResult(success=False, error=f"Calendar {action} failed: {exc}")

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(success=True, connector_name=self.name, records_synced=0)

    async def schema(self) -> dict[str, Any]:
        return {"table": "calendar_events", "mapping": {}, "schedule": "manual"}

    async def widgets(self) -> list[WidgetRecipe]:
        return [
            WidgetRecipe(
                title="Today's Calendar",
                display_type="feed",
                action="calendar_list",
                params={"days_ahead": 1, "max_results": 10},
                default_size="col-1 row-2",
                description="Events in the next 24 hours",
            ),
            WidgetRecipe(
                title="This Week",
                display_type="feed",
                action="calendar_list",
                params={"days_ahead": 7, "max_results": 15},
                default_size="col-1 row-2",
                description="Events in the next 7 days",
            ),
            WidgetRecipe(
                title="Calendar Stats",
                display_type="stats",
                action="calendar_summary",
                params={},
                default_size="col-1 row-1",
                description="Today / this week / next meeting at a glance",
            ),
        ]

    async def health(self, scope: ConnectorScope | None = None) -> ConnectorHealth:
        try:
            from pocketpaw.clients.gcalendar import CalendarClient

            client = CalendarClient()
            now = datetime.now(UTC)
            await client.list_events(time_min=now, time_max=now + timedelta(hours=1), max_results=1)
            return ConnectorHealth(
                ok=True,
                status=ConnectorStatus.CONNECTED,
                message="Calendar reachable",
                checked_at_ms=int(time.time() * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            return ConnectorHealth(
                ok=False,
                status=ConnectorStatus.ERROR,
                message=str(exc),
                checked_at_ms=int(time.time() * 1000),
            )
