# Google Calendar Client — HTTP client for Calendar API using OAuth tokens.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from pocketclaw.config import get_settings
from pocketclaw.integrations.oauth import OAuthManager
from pocketclaw.integrations.token_store import TokenStore

logger = logging.getLogger(__name__)

_CALENDAR_BASE = "https://www.googleapis.com/calendar/v3"


class CalendarClient:
    """HTTP client for Google Calendar API.

    Uses OAuth bearer tokens from the token store.
    """

    def __init__(self):
        self._oauth = OAuthManager(TokenStore())

    async def _get_token(self) -> str:
        """Get a valid OAuth access token for Calendar."""
        settings = get_settings()
        token = await self._oauth.get_valid_token(
            service="google_calendar",
            client_id=settings.google_oauth_client_id or "",
            client_secret=settings.google_oauth_client_secret or "",
        )
        if not token:
            raise RuntimeError(
                "Google Calendar not authenticated. Complete OAuth flow first "
                "(Settings > Google OAuth > Authorize Calendar)."
            )
        return token

    async def list_events(
        self,
        time_min: datetime | None = None,
        time_max: datetime | None = None,
        max_results: int = 10,
        calendar_id: str = "primary",
    ) -> list[dict[str, Any]]:
        """List upcoming calendar events.

        Args:
            time_min: Start of time range (default: now).
            time_max: End of time range (default: now + 7 days).
            max_results: Maximum number of events.
            calendar_id: Calendar ID (default: "primary").

        Returns:
            List of event dicts with id, summary, start, end, location, attendees.
        """
        token = await self._get_token()

        if time_min is None:
            time_min = datetime.now(UTC)
        if time_max is None:
            time_max = time_min + timedelta(days=7)

        params = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "maxResults": max_results,
            "singleEvents": "true",
            "orderBy": "startTime",
        }

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"{_CALENDAR_BASE}/calendars/{calendar_id}/events",
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        events = []
        for item in data.get("items", []):
            start = item.get("start", {})
            end = item.get("end", {})
            events.append(
                {
                    "id": item.get("id", ""),
                    "summary": item.get("summary", "(no title)"),
                    "start": start.get("dateTime", start.get("date", "")),
                    "end": end.get("dateTime", end.get("date", "")),
                    "location": item.get("location", ""),
                    "description": item.get("description", ""),
                    "attendees": [a.get("email", "") for a in item.get("attendees", [])],
                    "htmlLink": item.get("htmlLink", ""),
                }
            )

        return events

    async def create_event(
        self,
        summary: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
        attendees: list[str] | None = None,
        calendar_id: str = "primary",
    ) -> dict[str, Any]:
        """Create a calendar event.

        Args:
            summary: Event title.
            start: Start time in ISO 8601 format.
            end: End time in ISO 8601 format.
            description: Event description.
            location: Event location.
            attendees: List of attendee email addresses.
            calendar_id: Calendar ID (default: "primary").

        Returns:
            Dict with event id and htmlLink.
        """
        token = await self._get_token()

        body: dict[str, Any] = {
            "summary": summary,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        if attendees:
            body["attendees"] = [{"email": e} for e in attendees]

        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_CALENDAR_BASE}/calendars/{calendar_id}/events",
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            data = resp.json()

        return {
            "id": data.get("id", ""),
            "htmlLink": data.get("htmlLink", ""),
            "summary": data.get("summary", ""),
        }
