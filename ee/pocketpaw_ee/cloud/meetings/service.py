"""Meeting schedule service — CRUD + notification fan-out + realtime events.

Public API is module-level ``async def`` functions:

- ``create(...)`` — schedule a meeting, fan out notification + realtime event
- ``list_for_group(group_id)`` — list all meetings for a group
- ``list_upcoming_for_user(user_id, workspace_id)`` — upcoming meetings across
  all groups the user belongs to
- ``get(meeting_id)`` — get a single meeting by id
- ``update(meeting_id, user_id, ...)`` — update meeting fields
- ``cancel(meeting_id, user_id)`` — cancel (soft-delete) a meeting
- ``start_meeting(meeting_id, user_id)`` — transition to active, create LiveKit room
- ``end_meeting(meeting_id)`` — transition to ended (e.g. when LiveKit room empties)
- ``start_reminder_loop(app)`` — background task that sends reminders 5 min before
  scheduled meetings; call from the FastAPI lifespan
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler as _AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger as _DateTrigger
from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.realtime.emit import emit as emit_realtime
from pocketpaw_ee.cloud._core.realtime.events import (
    MeetingCancelled,
    MeetingScheduled,
    MeetingStarted,
    MeetingUpdated,
)
from pocketpaw_ee.cloud.chat.group_service import (
    _get_group_domain_or_404,
    _require_domain_group_member,
    list_member_ids,
)
from pocketpaw_ee.cloud.meetings.domain import MeetingSchedule as MeetingScheduleDomain
from pocketpaw_ee.cloud.meetings.dto import (
    CreateMeetingRequest,
    UpdateMeetingRequest,
)
from pocketpaw_ee.cloud.meetings.models import MeetingSchedule as MeetingScheduleDoc
from pocketpaw_ee.cloud.models.notification import NotificationSource
from pocketpaw_ee.cloud.notifications import service as notifications_service

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Doc → domain mapping
# ---------------------------------------------------------------------------


def _doc_to_domain(doc: MeetingScheduleDoc) -> MeetingScheduleDomain:
    return MeetingScheduleDomain(
        id=str(doc.id),
        workspace_id=doc.workspace,
        group_id=doc.group_id,
        created_by=doc.created_by,
        scheduled_at=doc.scheduled_at,
        duration_minutes=doc.duration_minutes,
        agenda=doc.agenda,
        status=doc.status,
        livekit_room_name=doc.livekit_room_name,
        created_at=getattr(doc, "createdAt", None) or datetime.now(UTC),
        updated_at=getattr(doc, "updatedAt", None) or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def create(
    *,
    workspace_id: str,
    user_id: str,
    user_name: str,
    body: CreateMeetingRequest,
) -> MeetingScheduleDomain:
    """Schedule a new meeting for a group.

    1. Verifies the caller is a group member.
    2. Inserts the ``MeetingSchedule`` doc.
    3. Posts a system message to the group chat.
    4. Fires a ``meeting.scheduled`` realtime event.
    5. Creates a ``notification.new`` for every group member.
    """
    # Verify membership
    group = await _get_group_domain_or_404(body.group_id)
    _require_domain_group_member(group, user_id)

    # Normalise to offset-naive UTC for consistent comparison with MongoDB values
    start_dt = body.scheduled_at
    if start_dt.tzinfo is not None:
        start_dt = start_dt.astimezone(UTC).replace(tzinfo=None)

    doc = MeetingScheduleDoc(
        workspace=workspace_id,
        group_id=body.group_id,
        created_by=user_id,
        scheduled_at=start_dt,  # already offset-naive UTC
        duration_minutes=body.duration_minutes,
        agenda=body.agenda,
        status="scheduled",
    )
    await doc.insert()
    domain = _doc_to_domain(doc)

    # Schedule APScheduler reminder + auto-start jobs
    schedule_meeting_jobs(doc)

    # Format the scheduled time for notification display
    scheduled_time_str = start_dt.strftime("%A, %B %d at %I:%M %p UTC")

    # Emit realtime event
    try:
        await emit_realtime(
            MeetingScheduled(
                data={
                    "meeting_id": domain.id,
                    "group_id": body.group_id,
                    "workspace_id": workspace_id,
                    "scheduled_at": start_dt.isoformat(),
                    "agenda": body.agenda,
                    "duration_minutes": body.duration_minutes,
                    "created_by": user_id,
                }
            )
        )
    except Exception as exc:
        logger.warning("Failed to emit meeting.scheduled event: %s", exc)

    # Fan out notifications to all group members (including creator)
    member_ids = await list_member_ids(body.group_id)
    for recipient_id in member_ids:
        try:
            await notifications_service.create(
                workspace_id=workspace_id,
                recipient=recipient_id,
                kind="meeting_scheduled",
                title=f"Meeting scheduled in {group.name}",
                body=f"{user_name} scheduled a meeting at {scheduled_time_str}"
                + (f" — {body.agenda}" if body.agenda else ""),
                source=NotificationSource(
                    type="meeting_scheduled",
                    id=domain.id,
                    room_id=body.group_id,
                ),
            )
        except Exception as exc:
            logger.warning("Failed to notify user %s about meeting: %s", recipient_id, exc)

    return domain


async def list_for_group(group_id: str) -> list[MeetingScheduleDomain]:
    """List all meetings for a group, newest first."""
    docs = (
        await MeetingScheduleDoc.find({"group_id": group_id})
        .sort("-createdAt")  # newest first
        .to_list()
    )
    return [_doc_to_domain(d) for d in docs]


async def list_upcoming_for_user(
    user_id: str,
    workspace_id: str,
    *,
    limit: int = 20,
) -> list[MeetingScheduleDomain]:
    """List upcoming (scheduled or active) meetings across all groups the
    user belongs to in this workspace.

    Uses the group membership list to scope the query, so the result only
    includes meetings the user can actually join.
    """
    from pocketpaw_ee.cloud.chat.group_service import list_groups

    groups = await list_groups(workspace_id, user_id)
    group_ids = [g.get("_id") for g in groups if isinstance(g, dict)]
    group_ids = [str(gid) for gid in group_ids if gid]  # normalize to str

    if not group_ids:
        return []

    now = datetime.now(UTC).replace(tzinfo=None)  # offset-naive for MongoDB comparison
    # Fetch scheduled + active meetings for the user's groups
    docs = (
        await MeetingScheduleDoc.find(
            {
                "group_id": {"$in": group_ids},
                "status": {"$in": ["scheduled", "active"]},
                # Only include meetings that haven't expired yet
                # (scheduled_at + duration_minutes >= now)
                "scheduled_at": {
                    "$gte": now - timedelta(hours=2)  # grace window for recent meetings
                },
            }
        )
        .sort("scheduled_at")  # ascending — earliest meetings first
        .limit(limit)
        .to_list()
    )
    return [_doc_to_domain(d) for d in docs]


async def get(meeting_id: str) -> MeetingScheduleDomain | None:
    """Get a single meeting by id."""
    doc = await MeetingScheduleDoc.get(PydanticObjectId(meeting_id))
    return _doc_to_domain(doc) if doc else None


async def update(
    meeting_id: str,
    user_id: str,
    body: UpdateMeetingRequest,
) -> MeetingScheduleDomain | None:
    """Update a meeting's fields.

    Only the creator can update.  Returns ``None`` if not found.
    """
    doc = await MeetingScheduleDoc.get(PydanticObjectId(meeting_id))
    if not doc:
        return None
    if doc.created_by != user_id:
        raise PermissionError("Only the meeting creator can update it")

    changed = False
    if body.scheduled_at is not None:
        val = body.scheduled_at
        if val.tzinfo is not None:
            val = val.astimezone(UTC).replace(tzinfo=None)
        doc.scheduled_at = val
        changed = True
    if body.duration_minutes is not None:
        doc.duration_minutes = body.duration_minutes
        changed = True
    if body.agenda is not None:
        doc.agenda = body.agenda
        changed = True
    if body.status is not None:
        doc.status = body.status
        changed = True

    if not changed:
        return _doc_to_domain(doc)

    await doc.save()
    domain = _doc_to_domain(doc)

    # Re-schedule APScheduler jobs if scheduled_at or status changed
    if body.scheduled_at is not None or body.status is not None:
        if body.status in ("cancelled", "ended"):
            unschedule_meeting_jobs(str(doc.id))
        else:
            schedule_meeting_jobs(doc)

    try:
        await emit_realtime(
            MeetingUpdated(
                data={
                    "meeting_id": domain.id,
                    "group_id": domain.group_id,
                    "scheduled_at": domain.scheduled_at.isoformat(),
                    "duration_minutes": domain.duration_minutes,
                    "agenda": domain.agenda,
                    "status": domain.status,
                }
            )
        )
    except Exception as exc:
        logger.warning("Failed to emit meeting.updated event: %s", exc)

    return domain


async def cancel(meeting_id: str, user_id: str) -> MeetingScheduleDomain | None:
    """Cancel a scheduled meeting.

    Only the creator can cancel.  Returns ``None`` if not found.
    """
    doc = await MeetingScheduleDoc.get(PydanticObjectId(meeting_id))
    if not doc:
        return None
    if doc.created_by != user_id:
        raise PermissionError("Only the meeting creator can cancel it")

    doc.status = "cancelled"
    await doc.save()
    domain = _doc_to_domain(doc)

    # Remove APScheduler jobs
    unschedule_meeting_jobs(meeting_id)

    try:
        await emit_realtime(
            MeetingCancelled(
                data={
                    "meeting_id": domain.id,
                    "group_id": domain.group_id,
                }
            )
        )
    except Exception as exc:
        logger.warning("Failed to emit meeting.cancelled event: %s", exc)

    # Notify group members about cancellation
    member_ids = await list_member_ids(domain.group_id)
    for recipient_id in member_ids:
        try:
            await notifications_service.create(
                workspace_id=domain.workspace_id,
                recipient=recipient_id,
                kind="meeting_cancelled",
                title="Meeting cancelled",
                body="A scheduled meeting in your group has been cancelled",
                source=NotificationSource(
                    type="meeting_cancelled",
                    id=domain.id,
                    room_id=domain.group_id,
                ),
            )
        except Exception as exc:
            logger.warning("Failed to notify user %s about cancellation: %s", recipient_id, exc)

    return domain


async def start_meeting(
    meeting_id: str,
    user_id: str,
    user_name: str,
) -> MeetingScheduleDomain | None:
    """Start a scheduled meeting by creating a LiveKit room.

    Only group members can start.  If the meeting is already active, returns
    the current state (idempotent).  Fires ``meeting.started`` realtime event
    so all members see a joinable call notification, and fans out a
    ``meeting_started`` notification to every group member.
    """
    doc = await MeetingScheduleDoc.get(PydanticObjectId(meeting_id))
    if not doc:
        return None

    group = await _get_group_domain_or_404(doc.group_id)
    _require_domain_group_member(group, user_id)

    return await _perform_start(doc, user_id, user_name)


async def _perform_start(
    doc: MeetingScheduleDoc,
    started_by: str,
    started_by_name: str,
) -> MeetingScheduleDomain:
    """Core start logic — transition to active, emit event, notify.

    Does **not** check membership; the caller (``start_meeting`` or
    ``_auto_start_meeting``) is responsible for authorisation.
    """
    room_name = f"meeting-{str(doc.id)}"

    if doc.status == "scheduled":
        doc.status = "active"
        doc.livekit_room_name = room_name
        await doc.save()
    elif doc.status == "active":
        pass  # idempotent
    else:
        raise ValueError(f"Cannot start meeting in status '{doc.status}'")

    domain = _doc_to_domain(doc)

    try:
        await emit_realtime(
            MeetingStarted(
                data={
                    "meeting_id": domain.id,
                    "group_id": domain.group_id,
                    "room_name": room_name,
                    "started_by": started_by,
                    "started_by_name": started_by_name,
                }
            )
        )
    except Exception as exc:
        logger.warning("Failed to emit meeting.started event: %s", exc)

    # Fan out meeting_started notification to all group members (including starter)
    group = await _get_group_domain_or_404(doc.group_id)
    member_ids = await list_member_ids(domain.group_id)
    for recipient_id in member_ids:
        try:
            await notifications_service.create(
                workspace_id=domain.workspace_id,
                recipient=recipient_id,
                kind="meeting_started",
                title=f"Meeting started in {group.name}",
                body=f"{started_by_name} started the meeting — join now",
                source=NotificationSource(
                    type="meeting_started",
                    id=domain.id,
                    room_id=domain.group_id,
                ),
            )
        except Exception as exc:
            logger.warning(
                "Failed to notify user %s about meeting started: %s",
                recipient_id,
                exc,
            )

    return domain


async def _auto_start_meeting(doc: MeetingScheduleDoc) -> None:
    """Auto-start a meeting at its scheduled time.

    Called by the background reminder loop when a meeting's ``scheduled_at``
    has arrived.  Transitions the meeting to active, creates the LiveKit room,
    emits a ``meeting.started`` realtime event, and fans out notifications
    with a join deep-link so every group member sees a "Join" action.
    """
    if doc.status != "scheduled":
        return

    room_name = f"meeting-{str(doc.id)}"
    doc.status = "active"
    doc.livekit_room_name = room_name
    await doc.save()

    domain = _doc_to_domain(doc)

    try:
        await emit_realtime(
            MeetingStarted(
                data={
                    "meeting_id": domain.id,
                    "group_id": domain.group_id,
                    "room_name": room_name,
                    "started_by": "system",
                    "started_by_name": "Auto",
                }
            )
        )
    except Exception as exc:
        logger.warning("Failed to emit auto-start meeting.started event: %s", exc)

    # Notify all group members with a join deep-link
    group = await _get_group_domain_or_404(doc.group_id)
    member_ids = await list_member_ids(domain.group_id)
    for recipient_id in member_ids:
        try:
            await notifications_service.create(
                workspace_id=domain.workspace_id,
                recipient=recipient_id,
                kind="meeting_started",
                title=f"Meeting starting now in {group.name}",
                body="Scheduled meeting is starting — join now",
                source=NotificationSource(
                    type="meeting_started",
                    id=domain.id,
                    room_id=domain.group_id,
                ),
            )
        except Exception as exc:
            logger.warning(
                "Failed to auto-start notify user %s: %s",
                recipient_id,
                exc,
            )


async def end_meeting(meeting_id: str) -> MeetingScheduleDomain | None:
    """End an active meeting (e.g. when the LiveKit room empties or on explicit end).

    Called by the LiveKit agent or by any group member via the API.
    """
    doc = await MeetingScheduleDoc.get(PydanticObjectId(meeting_id))
    if not doc:
        return None

    if doc.status == "active":
        doc.status = "ended"
        await doc.save()

    return _doc_to_domain(doc)


async def list_member_ids_for_meeting(meeting_id: str) -> list[str]:
    """Resolve the group member IDs for a meeting.

    Used by the audience resolver to fan out meeting events.
    """
    doc = await MeetingScheduleDoc.get(PydanticObjectId(meeting_id))
    if not doc:
        return []
    return await list_member_ids(doc.group_id)


# ---------------------------------------------------------------------------
# APScheduler-based reminder / auto-start (replaces polling loop)
#
# Each meeting schedules two precise DateTrigger jobs at create/update time
# and removes them on cancel/end:
#   1. Reminder job  — fires 5 min before ``scheduled_at``
#   2. Auto-start job — fires at ``scheduled_at``
#
# This eliminates the while-True DB-poll loop entirely. When no meetings
# are scheduled, no APScheduler jobs exist and no DB queries run.
# ---------------------------------------------------------------------------


_REMINDER_LEAD_TIME = timedelta(minutes=5)  # send 5 min before


async def _send_reminder(doc: MeetingScheduleDoc) -> None:
    """Send a ``meeting_reminder`` notification to all group members."""
    # Guard: only send if meeting is still scheduled (not already ended/cancelled)
    current = await MeetingScheduleDoc.get(doc.id)
    if current is None or current.status != "scheduled":
        return

    member_ids = await list_member_ids(doc.group_id)

    scheduled_time_str = doc.scheduled_at.strftime("%I:%M %p")
    duration_str = f"{doc.duration_minutes} min"

    for recipient_id in member_ids:
        try:
            await notifications_service.create(
                workspace_id=doc.workspace,
                recipient=recipient_id,
                kind="meeting_reminder",
                title=f"Meeting starting soon in {doc.scheduled_at.strftime('%b %d')}",
                body=f"Meeting starts at {scheduled_time_str} ({duration_str})"
                + (f" — {doc.agenda}" if doc.agenda else ""),
                source=NotificationSource(
                    type="meeting_reminder",
                    id=str(doc.id),
                    room_id=doc.group_id,
                ),
            )
        except Exception as exc:
            logger.warning(
                "Failed to send reminder for meeting %s to user %s: %s",
                doc.id,
                recipient_id,
                exc,
            )


# Singleton scheduler (shared across the module, lives for the app lifetime)
_scheduler: _AsyncIOScheduler | None = None


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


async def _schedule_auto_start(doc: MeetingScheduleDoc) -> None:
    """Transition a scheduled meeting to ``active`` at its ``scheduled_at``.

    This is the time-accurate counterpart to the old loop's check-tick.
    """
    # Guard: only auto-start if meeting is still scheduled
    current = await MeetingScheduleDoc.get(doc.id)
    if current is None or current.status != "scheduled":
        return

    await _auto_start_meeting(doc)


# ---------------------------------------------------------------------------
# Public scheduling API
# ---------------------------------------------------------------------------


def schedule_meeting_jobs(doc: MeetingScheduleDoc) -> None:
    """Schedule the reminder + auto-start APScheduler jobs for a meeting.

    Call after ``doc.insert()`` or after changing ``scheduled_at``.
    Idempotent — replaces existing jobs with the same ID.
    """
    sched = _get_scheduler()
    mid = str(doc.id)

    # CRITICAL: doc.scheduled_at is stored as timezone-naive UTC (stripped in
    # create()). APScheduler's DateTrigger interprets naive datetimes in the
    # server's LOCAL timezone, not UTC. We must attach UTC tzinfo so the job
    # fires at the correct absolute time regardless of server location.
    scheduled_at_utc = doc.scheduled_at.replace(tzinfo=UTC)

    # Reminder: 5 min before scheduled_at
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

    # Auto-start: at scheduled_at (with UTC tzinfo)
    sched.add_job(
        _schedule_auto_start,
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

    Called on startup to recover jobs that were lost during a server restart
    (APScheduler in-memory job store does not survive restarts). Queries
    MongoDB for all meetings with status ``scheduled`` whose ``scheduled_at``
    is still in the future and schedules reminder + auto-start jobs.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    docs = await MeetingScheduleDoc.find(
        {
            "status": "scheduled",
            "scheduled_at": {"$gte": now},
        }
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


def start_reminder_loop() -> asyncio.Task:
    """Recover APScheduler jobs for all future scheduled meetings on startup.

    Called from ``extensions.py``'s ``on_startup`` hook. Queries MongoDB for
    all ``scheduled`` meetings in the future and schedules per-meeting
    ``DateTrigger`` jobs for reminders (5 min before) and auto-start
    (at ``scheduled_at``).

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


__all__ = [
    "create",
    "list_for_group",
    "list_upcoming_for_user",
    "get",
    "update",
    "cancel",
    "start_meeting",
    "end_meeting",
    "list_member_ids_for_meeting",
    "start_reminder_loop",
    "schedule_meeting_jobs",
    "unschedule_meeting_jobs",
    "shutdown_scheduler",
]
