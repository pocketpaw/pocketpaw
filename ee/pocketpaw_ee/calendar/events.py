# Calendar module — bus event payloads.
# Created: 2026-05-19 (feat/calendar-module).
#
# Pydantic shapes for events emitted on event_bus. Names mirror the topic
# strings registered with the bus — keep them in lockstep when adding new
# events. The actual emit is done by service.py via event_bus.emit("topic",
# CalendarEventCreated(...).model_dump()).

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Topic constants — single source of truth for subscribers.
# ---------------------------------------------------------------------------

TOPIC_EVENT_CREATED = "calendar.event.created"
TOPIC_EVENT_UPDATED = "calendar.event.updated"
TOPIC_EVENT_DELETED = "calendar.event.deleted"
TOPIC_EVENT_STARTED = "calendar.event.started"
TOPIC_REMINDER_DUE = "calendar.reminder.due"
TOPIC_CONFLICT_DETECTED = "calendar.conflict.detected"


class CalendarEventCreated(BaseModel):
    event_id: str
    workspace_id: str
    calendar_id: str
    starts_at: datetime
    ends_at: datetime


class CalendarEventUpdated(BaseModel):
    event_id: str
    workspace_id: str
    calendar_id: str
    changes: dict[str, Any] = Field(default_factory=dict)


class CalendarEventDeleted(BaseModel):
    event_id: str
    workspace_id: str
    calendar_id: str


class CalendarEventStarted(BaseModel):
    """Fired by a future scheduler when an event reaches its start time."""

    event_id: str
    workspace_id: str


class ReminderDue(BaseModel):
    """Fired by a future scheduler N minutes before an event."""

    event_id: str
    workspace_id: str
    due_at: datetime
    channel: str  # e.g. "telegram", "slack", "email"


class ConflictDetected(BaseModel):
    event_id: str
    workspace_id: str
    conflicting_event_ids: list[str] = Field(default_factory=list)
