"""APScheduler-based reminder + auto-start for scheduled meetings.

Each meeting schedules two precise ``DateTrigger`` jobs at create time:
  1. Reminder job  — fires 5 min before ``scheduled_start``, sends
     ``meeting_reminder`` notification to all group members.
  2. Auto-start job — fires at ``scheduled_start``, calls
     ``scheduling.service.start_meeting()`` and emits ``meeting.started``.

This eliminates the while-True DB-poll loop entirely. When no meetings are
scheduled, no APScheduler jobs exist and no DB queries run.

Jobs are recovered from MongoDB on startup (APScheduler in-memory store
does not survive restarts).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler as _AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger as _DateTrigger

from pocketpaw_ee.cloud._core.realtime.emit import emit as emit_realtime
from pocketpaw_ee.cloud.meetings.events import MeetingReminder
from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc
from pocketpaw_ee.cloud.notifications.domain import NotificationSource

logger = logging.getLogger(__name__)

_REMINDER_LEAD_TIME = timedelta(minutes=5)  # send 5 min before scheduled_start

# Singleton scheduler (shared across the module, lives for the app lifetime)
_scheduler: _AsyncIOScheduler | None = None


# ---------------------------------------------------------------------------
# APScheduler helpers
# ---------------------------------------------------------------------------


def _get_scheduler() -> _AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = _AsyncIOScheduler()
        _scheduler.start()
    return _scheduler


def _reminder_job_id(meeting_id: str) -> str:
    return f"reminder:{meeting_id}"


def _autostart_job_id(meeting_id: str) -> str:
    return f"autostart:{meeting_id}"


# ---------------------------------------------------------------------------
# Job callbacks
# ---------------------------------------------------------------------------


async def _send_reminder(doc: _MeetingDoc) -> None:
    """Send a ``meeting_reminder`` notification to all group members."""
    # Re-fetch so we use the freshest status + data.
    meeting = await _MeetingDoc.get(doc.id)
    if meeting is None or meeting.status != "scheduled":
        return

    payload = meeting.raw_provider_payload or {}
    group_id = payload.get("group_id")
    if not group_id:
        logger.debug("No group_id for meeting %s — skipping reminder", doc.id)
        return

    member_ids = await _list_member_ids(group_id)
    if not member_ids:
        return

    scheduled = meeting.scheduled_start
    scheduled_time_str = scheduled.strftime("%I:%M %p") if scheduled else ""
    duration_min = payload.get("duration_minutes", 30)
    duration_str = f"{duration_min} min"

    for recipient_id in member_ids:
        try:
            from pocketpaw_ee.cloud.notifications import service as notifications_service

            await notifications_service.create(
                workspace_id=meeting.workspace,
                recipient=recipient_id,
                kind="meeting_reminder",
                title=f"Meeting starting soon{' — ' + meeting.title if meeting.title else ''}",
                body=(
                    f"Starts at {scheduled_time_str} ({duration_str})"
                    + (f" — {meeting.title}" if meeting.title else "")
                ),
                source=NotificationSource(
                    type="meeting_reminder",
                    id=str(meeting.id),
                    room_id=group_id,
                ),
            )
        except Exception as exc:
            logger.warning(
                "Failed to send reminder for meeting %s to user %s: %s",
                meeting.id,
                recipient_id,
                exc,
            )


async def _auto_start_meeting(doc: _MeetingDoc) -> None:
    """Auto-start a meeting at its scheduled time.

    Delegates to ``scheduling.service.start_meeting`` which handles
    provider dispatch, status change, and event emission.
    """
    # Guard: only auto-start if meeting is still scheduled
    current = await _MeetingDoc.get(doc.id)
    if current is None or current.status != "scheduled":
        return

    from pocketpaw_ee.cloud.meetings.scheduling import service as scheduling_service

    domain = await scheduling_service.start_meeting(doc.workspace, str(doc.id))
    if domain is None:
        return

    payload = doc.raw_provider_payload or {}
    group_id = payload.get("group_id")

    # Also emit MeetingReminder realtime event so the UI can show a toast
    try:
        await emit_realtime(
            MeetingReminder(
                data={
                    "workspace_id": doc.workspace,
                    "meeting_id": str(doc.id),
                    "source": doc.source,
                    "scheduled_start": (
                        doc.scheduled_start.isoformat() if doc.scheduled_start else ""
                    ),
                    "group_id": group_id,
                }
            )
        )
    except Exception:
        logger.exception("Failed to emit meeting.reminder for %s", doc.id)


# ---------------------------------------------------------------------------
# Public scheduling API
# ---------------------------------------------------------------------------


def schedule_meeting_jobs(doc: _MeetingDoc) -> None:
    """Schedule the reminder + auto-start APScheduler jobs for a meeting.

    Call after ``doc.insert()`` or after changing ``scheduled_start``.
    Idempotent — replaces existing jobs with the same ID.
    """
    sched = _get_scheduler()
    mid = str(doc.id)

    if not doc.scheduled_start:
        logger.debug("No scheduled_start for meeting %s — skipping job scheduling", mid)
        return

    # CRITICAL: doc.scheduled_start is stored as timezone-naive UTC.
    # APScheduler's DateTrigger interprets naive datetimes in the server's
    # LOCAL timezone, not UTC. We must attach UTC tzinfo so the job fires
    # at the correct absolute time regardless of server location.
    scheduled_at_utc = doc.scheduled_start.replace(tzinfo=UTC)

    # Reminder: 5 min before scheduled_start
    reminder_at = scheduled_at_utc - _REMINDER_LEAD_TIME
    if reminder_at > datetime.now(UTC):
        sched.add_job(
            _send_reminder,
            trigger=_DateTrigger(run_date=reminder_at),
            args=[doc],
            id=_reminder_job_id(mid),
            replace_existing=True,
        )
    else:
        logger.debug("Skipping reminder job for %s — reminder time already passed", mid)

    # Auto-start: at scheduled_start (with UTC tzinfo)
    sched.add_job(
        _auto_start_meeting,
        trigger=_DateTrigger(run_date=scheduled_at_utc),
        args=[doc],
        id=_autostart_job_id(mid),
        replace_existing=True,
    )


def unschedule_meeting_jobs(meeting_id: str) -> None:
    """Remove a meeting's APScheduler jobs (on cancel / status change)."""
    sched = _get_scheduler()
    for job_id in (_reminder_job_id(meeting_id), _autostart_job_id(meeting_id)):
        try:
            sched.remove_job(job_id)
        except Exception:
            pass  # job may not exist


async def _recover_jobs_on_startup() -> None:
    """Re-schedule APScheduler jobs for all future scheduled meetings.

    Called on startup to recover jobs that were lost during a server restart.
    Queries MongoDB for all meetings with status ``scheduled`` whose
    ``scheduled_start`` is still in the future and schedules reminder +
    auto-start jobs.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    docs = await _MeetingDoc.find(
        {"status": "scheduled", "scheduled_start": {"$gte": now}}
    ).to_list()

    count = 0
    for doc in docs:
        try:
            schedule_meeting_jobs(doc)
            count += 1
        except Exception as exc:
            logger.warning("Failed to re-schedule meeting %s on startup: %s", doc.id, exc)

    if count:
        logger.info("Re-scheduled %d meeting job(s) from DB on startup", count)


# ---------------------------------------------------------------------------
# Lifecycle — called from extensions.py
# ---------------------------------------------------------------------------


def start_reminder_loop() -> asyncio.Task:
    """Recover APScheduler jobs for all future scheduled meetings on startup.

    Called from ``extensions.py``'s ``on_startup`` hook. Queries MongoDB for
    all ``scheduled`` meetings in the future and schedules per-meeting
    ``DateTrigger`` jobs for reminders (5 min before) and auto-start
    (at ``scheduled_start``).

    Returns an asyncio Task so the existing lifespan code in extensions.py
    doesn't break. The task completes after recovery (no long-running loop).
    """
    logger.info(
        "Meeting reminder system initialised (APScheduler per-meeting jobs; recovering from DB)"
    )

    async def _recover() -> None:
        await _recover_jobs_on_startup()

    return asyncio.create_task(_recover())


async def shutdown_scheduler() -> None:
    """Gracefully shut down the APScheduler (called from extensions.py on_shutdown)."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        logger.info("Meeting APScheduler shut down")


async def _list_member_ids(group_id: str) -> list[str]:
    """List user ids for all members of a group. Returns [] on any error."""
    try:
        from pocketpaw_ee.cloud.chat import group_service

        return await group_service.list_member_ids(group_id)
    except Exception:
        logger.exception("Failed to list members for group=%s", group_id)
        return []
