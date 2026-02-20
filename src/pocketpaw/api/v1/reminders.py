# Reminders router — list, add, delete.
# Created: 2026-02-20

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from pocketpaw.api.v1.schemas.reminders import (
    AddReminderRequest,
    AddReminderResponse,
    DeleteReminderResponse,
    ReminderInfo,
    ReminderListResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Reminders"])


@router.get("/reminders", response_model=ReminderListResponse)
async def list_reminders():
    """Get all active reminders."""
    from pocketpaw.scheduler import get_scheduler

    sched = get_scheduler()
    raw = sched.get_reminders()

    reminders = []
    for r in raw:
        reminders.append(
            ReminderInfo(
                id=r.get("id", ""),
                text=r.get("text", ""),
                trigger_at=r.get("trigger_at", ""),
                created_at=r.get("created_at", ""),
                time_remaining=sched.format_time_remaining(r),
            )
        )

    return ReminderListResponse(reminders=reminders)


@router.post("/reminders", response_model=AddReminderResponse)
async def add_reminder(body: AddReminderRequest):
    """Add a reminder from natural language (e.g. 'in 5 minutes to call mom')."""
    from pocketpaw.scheduler import get_scheduler

    sched = get_scheduler()
    result = sched.add_reminder(body.message)

    if result is None:
        raise HTTPException(
            status_code=400,
            detail="Could not parse time from message. Try 'in 5 minutes' or 'at 3pm'",
        )

    return AddReminderResponse(
        reminder=ReminderInfo(
            id=result.get("id", ""),
            text=result.get("text", ""),
            trigger_at=result.get("trigger_at", ""),
            created_at=result.get("created_at", ""),
            time_remaining=sched.format_time_remaining(result),
        )
    )


@router.delete("/reminders/{reminder_id}", response_model=DeleteReminderResponse)
async def delete_reminder(reminder_id: str):
    """Delete a reminder by ID."""
    from pocketpaw.scheduler import get_scheduler

    sched = get_scheduler()
    deleted = sched.delete_reminder(reminder_id)

    if not deleted:
        raise HTTPException(status_code=404, detail="Reminder not found")

    return DeleteReminderResponse(id=reminder_id)
