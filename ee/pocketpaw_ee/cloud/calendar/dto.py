# dto.py — Cloud calendar entity response schema.
#
# Created: 2026-05-24 (feat/calendar-entity-surface, #1214) — Pydantic
# response model for the wire shape. Kept separate from the domain per
# the ee/cloud rule "DTOs separate request and response" (#4) — even
# though we have no request type yet, the response stays in its own
# file so the surface mirrors the canonical entity layout (pockets,
# cycles, etc.) and future read-filter DTOs land beside it.
#
# No request schema — ``list_upcoming`` takes scalar parameters
# (workspace_id / user_id / limit) rather than a request body. If a
# future caller needs a richer filter (date range, calendar id),
# introduce ``ListUpcomingRequest`` here at that point.

from __future__ import annotations

from pydantic import BaseModel, Field


class CalendarEventResponse(BaseModel):
    """Wire shape of a single calendar event surfaced to the agent.

    Mirrors the ``CalendarEvent`` domain object 1-to-1 — no field
    renames or transforms. Kept as a distinct Pydantic model so the
    HTTP wire contract stays stable independent of how the in-process
    domain object evolves.
    """

    id: str
    workspace_id: str
    title: str
    start: str  # ISO 8601 (RFC 3339 datetime or YYYY-MM-DD date)
    end: str
    source: str
    attendees: list[str] = Field(default_factory=list)


__all__ = ["CalendarEventResponse"]
