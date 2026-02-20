# Reminder schemas.
# Created: 2026-02-20

from __future__ import annotations

from pydantic import BaseModel, Field


class ReminderInfo(BaseModel):
    """A single reminder entry."""

    id: str
    text: str
    trigger_at: str
    created_at: str
    time_remaining: str = ""


class ReminderListResponse(BaseModel):
    """List of active reminders."""

    reminders: list[ReminderInfo]


class AddReminderRequest(BaseModel):
    """Create a reminder from natural language."""

    message: str = Field(..., min_length=1, max_length=5000)


class AddReminderResponse(BaseModel):
    """Newly created reminder."""

    reminder: ReminderInfo


class DeleteReminderResponse(BaseModel):
    """Confirmation of deleted reminder."""

    id: str
    deleted: bool = True
