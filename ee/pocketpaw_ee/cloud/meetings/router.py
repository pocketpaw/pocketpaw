"""FastAPI REST router for meeting scheduling.

Endpoints:
- ``POST /api/v1/meetings`` — schedule a new meeting
- ``GET /api/v1/meetings`` — list meetings (by ``group_id`` or ``upcoming``)
- ``GET /api/v1/meetings/{id}`` — get a single meeting
- ``PATCH /api/v1/meetings/{id}`` — update a meeting
- ``DELETE /api/v1/meetings/{id}`` — cancel a meeting
- ``POST /api/v1/meetings/{id}/start`` — start a meeting (create LiveKit room)
- ``POST /api/v1/meetings/{id}/end`` — end an active meeting

All routes require an active enterprise license and user authentication.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.dto import (
    CreateMeetingRequest,
    MeetingScheduleOut,
    UpdateMeetingRequest,
    meeting_to_dto,
)
from pocketpaw_ee.cloud.shared.deps import current_user, current_workspace_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/meetings", tags=["Meetings"])


# ---------------------------------------------------------------------------
# Helper — get the user's display name from the FastAPI user object
# ---------------------------------------------------------------------------


def _user_name(user: Any) -> str:
    return getattr(user, "full_name", None) or getattr(user, "email", None) or str(user.id)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("", response_model=MeetingScheduleOut, status_code=201)
async def create_meeting(
    body: CreateMeetingRequest,
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """Schedule a new meeting for a group.

    Verifies the caller is a group member, checks for overlaps,
    posts a system message to the group, and fans out notifications.
    """
    await require_license()
    try:
        domain = await meetings_service.create(
            workspace_id=workspace_id,
            user_id=str(user.id),
            user_name=_user_name(user),
            body=body,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return meeting_to_dto(domain)


@router.get("", response_model=list[MeetingScheduleOut])
async def list_meetings(
    group_id: str | None = Query(None, description="Filter by group"),
    upcoming: bool = Query(False, description="Return upcoming meetings for current user"),
    user=Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    """List meetings.

    - With ``?group_id=X``: all meetings for that group.
    - With ``?upcoming=true``: upcoming (scheduled/active) meetings across all
      groups the caller belongs to.
    - Without filters: all upcoming meetings (same as ``?upcoming=true``).
    """
    await require_license()

    if group_id:
        domains = await meetings_service.list_for_group(group_id)
    else:
        # Default: return upcoming meetings for the user
        domains = await meetings_service.list_upcoming_for_user(str(user.id), workspace_id)

    return [meeting_to_dto(d) for d in domains]


@router.get("/{meeting_id}", response_model=MeetingScheduleOut)
async def get_meeting(
    meeting_id: str,
    user=Depends(current_user),
):
    """Get a single meeting by ID."""
    await require_license()
    domain = await meetings_service.get(meeting_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting_to_dto(domain)


@router.patch("/{meeting_id}", response_model=MeetingScheduleOut)
async def update_meeting(
    meeting_id: str,
    body: UpdateMeetingRequest,
    user=Depends(current_user),
):
    """Update a meeting.  Only the creator can update."""
    await require_license()
    try:
        domain = await meetings_service.update(meeting_id, str(user.id), body)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not domain:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting_to_dto(domain)


@router.delete("/{meeting_id}", response_model=MeetingScheduleOut)
async def cancel_meeting(
    meeting_id: str,
    user=Depends(current_user),
):
    """Cancel a scheduled meeting.  Only the creator can cancel."""
    await require_license()
    try:
        domain = await meetings_service.cancel(meeting_id, str(user.id))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not domain:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting_to_dto(domain)


@router.post("/{meeting_id}/start", response_model=MeetingScheduleOut)
async def start_meeting(
    meeting_id: str,
    user=Depends(current_user),
):
    """Start a scheduled meeting.

    Transitions the meeting to ``active`` and fires a ``meeting.started``
    realtime event so all group members see a joinable call notification.
    The caller must be a member of the meeting's group.
    """
    await require_license()
    try:
        domain = await meetings_service.start_meeting(meeting_id, str(user.id), _user_name(user))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    if not domain:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting_to_dto(domain)


@router.post("/{meeting_id}/end", response_model=MeetingScheduleOut)
async def end_meeting(
    meeting_id: str,
    user=Depends(current_user),
):
    """End an active meeting."""
    await require_license()
    domain = await meetings_service.end_meeting(meeting_id)
    if not domain:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return meeting_to_dto(domain)
