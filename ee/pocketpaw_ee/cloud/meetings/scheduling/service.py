"""Meeting lifecycle — source-agnostic scheduling helpers.

Used by ``reminders.py`` (APScheduler) and by the manual start/end
endpoints (when they land). Each function:
  1. Loads the Meeting doc
  2. Resolves the source-specific provider
  3. Dispatches to ``provider.start()`` / ``provider.end()``
  4. Updates status
  5. Emits the corresponding bus event so bridges (notifications, calendar)
     can react.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace

from pocketpaw_ee.cloud._core.realtime.emit import emit as emit_realtime
from pocketpaw_ee.cloud.meetings.domain import Meeting as MeetingDomain
from pocketpaw_ee.cloud.meetings.events import MeetingEnded, MeetingStarted
from pocketpaw_ee.cloud.meetings.providers import base as providers_base
from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc

logger = logging.getLogger(__name__)


async def start_meeting(workspace_id: str, meeting_id: str) -> MeetingDomain | None:
    """Transition a meeting to ``in_progress``.

    Loads the doc, resolves the provider, calls ``provider.start()``,
    persists the status change, emits ``meeting.started``, and returns
    the domain object. Idempotent — safe to call from both the APScheduler
    auto-start job and manual API endpoints.
    """
    doc = await _MeetingDoc.get(meeting_id)
    if not doc:
        logger.warning("start_meeting: meeting %s not found", meeting_id)
        return None

    if doc.status != "scheduled":
        logger.debug("start_meeting: meeting %s status is %s — skipping", meeting_id, doc.status)
        return None

    # Resolve the provider and call start()
    provider_impl = providers_base.resolve(doc.source)
    ctx = SimpleNamespace(workspace_id=workspace_id, user_id=doc.created_by_user_id or "")
    try:
        result = await provider_impl.start(ctx, doc)
    except Exception:
        logger.exception("Provider start() failed for meeting %s", meeting_id)
        return None

    # Merge provider_payload_updates into raw_provider_payload
    updates = result.provider_payload_updates or {}
    if updates:
        doc.raw_provider_payload = {**(doc.raw_provider_payload or {}), **updates}

    doc.status = "in_progress"
    doc.actual_start = datetime.now(UTC)
    await doc.save()

    logger.info("Meeting %s started (source=%s)", meeting_id, doc.source)

    # Emit on both buses so the notification bridge (event_bus) AND the
    # frontend (realtime WebSocket) receive the meeting.started event.
    from pocketpaw_ee.cloud.shared.events import event_bus as _event_bus

    started_data = {
        "workspace_id": workspace_id,
        "meeting_id": meeting_id,
        "source": doc.source,
        "join_url": doc.join_url,
        "group_id": (doc.raw_provider_payload or {}).get("group_id"),
    }
    try:
        await emit_realtime(MeetingStarted(data=started_data))
    except Exception:
        logger.exception("Failed to emit realtime meeting.started for %s", meeting_id)
    try:
        await _event_bus.emit("meeting.started", started_data)
    except Exception:
        logger.exception("Failed to emit bus meeting.started for %s", meeting_id)

    return _doc_to_domain(doc)


async def end_meeting(workspace_id: str, meeting_id: str) -> MeetingDomain | None:
    """Transition a meeting to ``ended``.

    Loads the doc, resolves the provider, calls ``provider.end()``,
    persists the status change, emits ``meeting.ended``.
    """
    doc = await _MeetingDoc.get(meeting_id)
    if not doc:
        logger.warning("end_meeting: meeting %s not found", meeting_id)
        return None

    if doc.status in ("ended", "cancelled"):
        logger.debug("end_meeting: meeting %s already %s — skipping", meeting_id, doc.status)
        return None

    provider_impl = providers_base.resolve(doc.source)
    ctx = SimpleNamespace(workspace_id=workspace_id, user_id=doc.created_by_user_id or "")
    try:
        await provider_impl.end(ctx, doc)
    except Exception:
        logger.exception("Provider end() failed for meeting %s", meeting_id)
        # Continue — mark as ended even if provider cleanup fails

    doc.status = "ended"
    doc.actual_end = datetime.now(UTC)
    await doc.save()

    logger.info("Meeting %s ended (source=%s)", meeting_id, doc.source)

    try:
        await emit_realtime(
            MeetingEnded(
                data={
                    "workspace_id": workspace_id,
                    "meeting_id": meeting_id,
                    "source": doc.source,
                    "actual_end": doc.actual_end.isoformat(),
                }
            )
        )
    except Exception:
        logger.exception("Failed to emit meeting.ended for %s", meeting_id)

    return _doc_to_domain(doc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _doc_to_domain(doc: _MeetingDoc) -> MeetingDomain:
    """Convert a Beanie doc to the frozen domain dataclass."""
    return MeetingDomain(
        id=str(doc.id),
        workspace_id=doc.workspace,
        source=doc.source,
        provider=doc.provider,
        provider_meeting_id=doc.provider_meeting_id,
        provider_space_id=doc.provider_space_id,
        title=doc.title,
        join_url=doc.join_url,
        organizer_email=doc.organizer_email,
        scheduled_start=doc.scheduled_start,
        scheduled_end=doc.scheduled_end,
        actual_start=doc.actual_start,
        actual_end=doc.actual_end,
        status=doc.status,
        participants=tuple(doc.participants),
        recording_file_ids=tuple(doc.recording_file_ids),
        created_by_user_id=doc.created_by_user_id,
        created_at=doc.createdAt,
        updated_at=doc.updatedAt,
    )
