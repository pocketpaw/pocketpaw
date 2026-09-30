# Meetings — lobby: guests ask to join, people in the call admit or deny.
# Created: 2026-10-01 (feat/meetings-lobby, MC-3).
#
# A guest with a meeting link knocks (public). The knock row keeps only a
# sha256 of a per-knock bearer secret the guest gets once; every guest read or
# cancel must present it, and anything that doesn't match (unknown id, other
# code, wrong or missing secret) is the same 404. A member of the meeting room
# who is IN the call (LiveKit presence: their identity is their user id) admits
# or denies; the transition is one conditional update on status "waiting", so
# two people clicking at once get one decision and one 409.
#
# The guest's LiveKit token (livekit/invites.issue_guest_token: guest-<hex>,
# this room only) is minted only while the meeting is open and a MEMBER is in
# the call (``_member_in_call``: neither call-bot nor guest-*). LiveKit creates a
# missing room on connect, so a token for an empty room would let a guest start
# the call alone: no budget gate, no row. Guests don't count, or admitted guests
# could keep a call going and let others in with no member present.
#
# Time rules, computed on read (no sweep): a waiting knock expires 10 minutes
# after it was made or as soon as the meeting closes; an admission lasts an hour.
# Guest tokens live 5 minutes (never past the admission): every poll mints a
# fresh one, and a short token limits what a leaked one can do. A TTL index
# drops every row after a day.
# ``access="open"`` admits a knock on its own once the call is running, at knock
# time or on the guest's next poll.
#
# Every admit / deny is written to the audit log (``meeting.knock_decide``).
#
# Events (realtime, to the meeting room's members): ``meeting.knock`` for a new
# knock that needs a decision, ``meeting.knock_resolved`` whenever a knock leaves
# "waiting" (admitted, denied, cancelled, expired).

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta

from beanie import PydanticObjectId

from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, Forbidden, NotFound
from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.dto import (
    KnockCreatedResponse,
    KnockDecisionResponse,
    KnockRequest,
    KnockStatusResponse,
    KnockSummaryResponse,
)
from pocketpaw_ee.cloud.meetings.events import MeetingKnock as MeetingKnockEvent
from pocketpaw_ee.cloud.meetings.events import MeetingKnockResolved
from pocketpaw_ee.cloud.models.meeting import Meeting as _MeetingDoc
from pocketpaw_ee.cloud.models.meeting import MeetingKnock as _KnockDoc

logger = logging.getLogger(__name__)

KNOCK_TTL = timedelta(minutes=10)
ADMIT_TTL = timedelta(hours=1)
# Each poll mints a fresh token, so it can be short; never past the admission.
GUEST_TOKEN_TTL = timedelta(minutes=5)

_aware = meetings_service._aware


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _decided() -> ConflictError:
    return ConflictError("meeting.knock_decided", "Someone already answered this request.")


def _ended() -> CloudError:
    return CloudError(410, "meeting.ended", "This meeting has ended.")


async def _emit_resolved(knock: _KnockDoc, meeting: _MeetingDoc) -> None:
    await emit(
        MeetingKnockResolved(
            data={
                "workspace_id": meeting.workspace,
                "meeting_id": str(meeting.id),
                "group_id": meetings_service._room_of(meeting),
                "knock_id": str(knock.id),
                "status": knock.status,
            }
        )
    )


async def _transition(
    knock: _KnockDoc,
    meeting: _MeetingDoc,
    status: str,
    *,
    decided_by: str | None = None,
    from_status: str = "waiting",
) -> bool:
    """Move a knock out of ``from_status`` if nobody beat us to it.

    One conditional update, so concurrent deciders can't both win. Returns
    False (and reloads ``knock``) when the knock had already moved. An admission
    that ages out keeps its ``decided_by`` / ``decided_at``.
    """
    changes: dict = {"status": status}
    if from_status == "waiting":
        changes |= {"decided_at": _now(), "decided_by": decided_by}
    result = await _KnockDoc.find_one({"_id": knock.id, "status": from_status}).update(
        {"$set": changes}
    )
    if not result.modified_count:
        fresh = await _KnockDoc.get(knock.id)
        if fresh is not None:
            knock.status = fresh.status
            knock.decided_at = fresh.decided_at
            knock.decided_by = fresh.decided_by
        return False
    for field, value in changes.items():
        setattr(knock, field, value)
    await _emit_resolved(knock, meeting)
    return True


async def _age_out(knock: _KnockDoc, meeting: _MeetingDoc) -> _KnockDoc:
    """Apply the time rules: stale waits and old admissions read as expired."""
    now = _now()
    if knock.status == "waiting" and (
        _aware(knock.created_at) + KNOCK_TTL <= now or meetings_service._is_closed(meeting, now)
    ):
        await _transition(knock, meeting, "expired")
    elif knock.status == "admitted" and _aware(knock.decided_at) + ADMIT_TTL <= now:
        await _transition(knock, meeting, "expired", from_status="admitted")
    return knock


async def _open_meeting_for_guests(code: str) -> tuple[_MeetingDoc, str]:
    """The coded meeting and its room; 404 unknown, 410 closed."""
    meeting = await meetings_service._find_by_code(code)
    room_id = meetings_service._room_of(meeting)
    if not room_id:
        raise NotFound("meeting", code)
    if meetings_service._is_closed(meeting, _now()):
        raise _ended()
    return meeting, room_id


async def _guest_knock(
    code: str, knock_id: str, secret: str | None
) -> tuple[_KnockDoc, _MeetingDoc]:
    """The knock, if the code, id and secret all match. One 404 for every miss."""
    miss = NotFound("meeting_knock")
    try:
        oid = PydanticObjectId(knock_id)
    except Exception:
        raise miss from None
    try:
        meeting = await meetings_service._find_by_code(code)
    except NotFound:
        raise miss from None  # don't tell a knock-id prober which codes exist
    knock = await _KnockDoc.find_one(
        {"_id": oid, "meeting": str(meeting.id), "workspace": meeting.workspace}
    )
    if knock is None or not hmac.compare_digest(knock.secret_hash, _hash(secret or "")):
        raise miss
    return knock, meeting


async def _call_identities(room_id: str) -> list[str]:
    """LiveKit identities in the room's call. Raises when LiveKit can't be read."""
    from pocketpaw_ee.cloud.livekit import service as livekit_service

    info = await livekit_service.get_room_info(room_id)
    return [p.get("identity", "") for p in (info or {}).get("participants", [])]


async def _member_in_call(room_id: str) -> bool:
    """True while a MEMBER is in the call: someone neither ``call-bot`` nor a
    ``guest-*``. The lobby's gates use this instead of ``get_room_info().active``,
    which counts guests, so admitted guests can't keep letting people in (or
    keep minting themselves tokens) once every member has left."""
    try:
        identities = await _call_identities(room_id)
    except Exception:
        logger.debug("Room presence check failed for %s", room_id, exc_info=True)
        return False
    return any(i != "call-bot" and not i.startswith("guest-") for i in identities)


async def _auto_admit_if_open(knock: _KnockDoc, meeting: _MeetingDoc) -> None:
    if (
        knock.status == "waiting"
        and meeting.access == "open"
        and await _member_in_call(meetings_service._room_of(meeting) or "")
    ):
        await _transition(knock, meeting, "admitted")


# ---------------------------------------------------------------------------
# Guest side (public)
# ---------------------------------------------------------------------------


async def knock(code: str, body: KnockRequest) -> KnockCreatedResponse:
    """A guest asks to join. 404 unknown code, 410 closed meeting, 403
    ``meeting.email_not_allowed`` when the meeting has a guest list and the email
    isn't on it (trimmed, case-insensitive)."""
    body = KnockRequest.model_validate(body)
    meeting, _room = await _open_meeting_for_guests(code)
    email = (body.email or "").strip().lower() or None
    allowed = {e.strip().lower() for e in meeting.guest_emails if e.strip()}
    if allowed and email not in allowed:
        raise Forbidden(
            "meeting.email_not_allowed",
            "This email isn't on the guest list. Check with the meeting host.",
        )

    from pocketpaw_ee.cloud.livekit.invites import new_guest_identity

    secret = secrets.token_urlsafe(32)
    row = _KnockDoc(
        meeting=str(meeting.id),
        workspace=meeting.workspace,
        name=body.name,
        email=email,
        guest_identity=new_guest_identity(),
        secret_hash=_hash(secret),
    )
    await row.insert()

    if meeting.access == "open":
        # Nothing for anyone to decide; admitted now if the call runs, else on
        # the guest's first poll after someone joins.
        await _auto_admit_if_open(row, meeting)
    else:
        await emit(
            MeetingKnockEvent(
                data={
                    "workspace_id": meeting.workspace,
                    "meeting_id": str(meeting.id),
                    "group_id": meetings_service._room_of(meeting),
                    "knock_id": str(row.id),
                    "name": row.name,
                }
            )
        )
    return KnockCreatedResponse(knock_id=str(row.id), secret=secret, status=row.status)


async def knock_status(code: str, knock_id: str, secret: str | None) -> KnockStatusResponse:
    """The guest's poll. Carries a fresh LiveKit token while admitted, the meeting
    is open and a member is in the call; just ``status`` otherwise."""
    knock_row, meeting = await _guest_knock(code, knock_id, secret)
    await _age_out(knock_row, meeting)
    await _auto_admit_if_open(knock_row, meeting)
    out = KnockStatusResponse(status=knock_row.status)
    if knock_row.status != "admitted" or meetings_service._is_closed(meeting, _now()):
        return out
    room_id = meetings_service._room_of(meeting) or ""
    if not await _member_in_call(room_id):
        return out

    from pocketpaw_ee.cloud.livekit import service as livekit_service
    from pocketpaw_ee.cloud.livekit.invites import issue_guest_token

    left = _aware(knock_row.decided_at) + ADMIT_TTL - _now()
    ttl = int(min(GUEST_TOKEN_TTL, left).total_seconds())
    if ttl <= 0:
        return out
    room_name = livekit_service.room_name_for_group(room_id)
    out.token = await issue_guest_token(
        room_name, knock_row.guest_identity, knock_row.name, ttl_seconds=ttl
    )
    out.room_name = room_name
    out.identity = knock_row.guest_identity
    out.livekit_url = livekit_service.LIVEKIT_URL
    return out


async def cancel_knock(code: str, knock_id: str, secret: str | None) -> KnockStatusResponse:
    """The guest gives up waiting. 409 ``meeting.knock_decided`` once answered."""
    knock_row, meeting = await _guest_knock(code, knock_id, secret)
    await _age_out(knock_row, meeting)
    if not await _transition(knock_row, meeting, "cancelled"):
        raise _decided()
    return KnockStatusResponse(status="cancelled")


# ---------------------------------------------------------------------------
# Member side
# ---------------------------------------------------------------------------


async def _member_meeting(workspace_id: str, user_id: str, meeting_id: str) -> _MeetingDoc:
    """The meeting in this workspace (404) whose room the caller is in (403)."""
    from pocketpaw_ee.cloud.livekit import service as livekit_service

    try:
        oid = PydanticObjectId(meeting_id)
    except Exception:
        raise NotFound("meeting", meeting_id) from None
    meeting = await _MeetingDoc.find_one({"_id": oid, "workspace": workspace_id})
    room_id = meetings_service._room_of(meeting) if meeting else None
    if meeting is None or not room_id:
        raise NotFound("meeting", meeting_id)
    await livekit_service.require_call_group(room_id, user_id, workspace_id)
    return meeting


async def _require_in_call(meeting: _MeetingDoc, user_id: str) -> None:
    """403 ``meeting.not_in_call`` unless the caller is in the call right now."""
    if user_id not in await _call_identities(meetings_service._room_of(meeting) or ""):
        raise Forbidden("meeting.not_in_call", "Join the call to see who's waiting.")


async def list_knocks(
    workspace_id: str, user_id: str, meeting_id: str
) -> list[KnockSummaryResponse]:
    """Guests still waiting, oldest first. A member of the meeting room who is in
    the call (403 ``livekit.room_forbidden`` / ``meeting.not_in_call``)."""
    meeting = await _member_meeting(workspace_id, user_id, meeting_id)
    await _require_in_call(meeting, user_id)
    rows = (
        await _KnockDoc.find(
            {"meeting": str(meeting.id), "workspace": workspace_id, "status": "waiting"}
        )
        .sort("created_at")
        .to_list()
    )
    out = []
    for row in rows:
        await _age_out(row, meeting)
        if row.status == "waiting":
            out.append(
                KnockSummaryResponse(
                    knock_id=str(row.id), name=row.name, email=row.email, created_at=row.created_at
                )
            )
    return out


async def decide_knock(
    workspace_id: str, user_id: str, meeting_id: str, knock_id: str, *, admit: bool
) -> KnockDecisionResponse:
    """Admit or deny. The caller must be a member of the meeting room (403
    ``livekit.room_forbidden``) AND in the call right now (403
    ``meeting.not_in_call``). 410 closed meeting, 404 unknown knock, 409
    ``meeting.knock_decided`` when it was already answered, cancelled or expired."""
    meeting = await _member_meeting(workspace_id, user_id, meeting_id)
    if meetings_service._is_closed(meeting, _now()):
        raise _ended()
    await _require_in_call(meeting, user_id)

    try:
        oid = PydanticObjectId(knock_id)
    except Exception:
        raise NotFound("meeting_knock") from None
    row = await _KnockDoc.find_one(
        {"_id": oid, "meeting": str(meeting.id), "workspace": workspace_id}
    )
    if row is None:
        raise NotFound("meeting_knock")
    await _age_out(row, meeting)
    status = "admitted" if admit else "denied"
    if not await _transition(row, meeting, status, decided_by=user_id):
        raise _decided()

    from pocketpaw_ee.guards import audit

    audit.log_privileged_action(
        actor=user_id,
        action="meeting.knock_decide",
        resource_id=str(row.id),
        workspace_id=workspace_id,
        meeting_id=str(meeting.id),
        decision=status,
    )
    return KnockDecisionResponse(knock_id=str(row.id), status=status)
