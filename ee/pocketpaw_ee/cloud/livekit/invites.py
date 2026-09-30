"""Meeting invite service — shareable links for guest access to LiveKit calls.

Guests receive a temporary LiveKit access token with ``guest-`` prefixed
identity, bypassing the group membership check that guards regular
participant tokens. The plaintext invite token lives only in the shared URL;
we persist ``sha256(plaintext)`` so a DB read cannot reconstruct a usable link.

2026-09-30 (fix/livekit-call-security): the guest email allow-list is enforced
here at accept time. The call invite modal sends it inside the ``display_name``
JSON (``{"emails": [...], "description": ...}``); create moves it to
``MeetingInvite.allowed_emails``, legacy rows are parsed on read, validate
returns ``requires_email`` instead of the list, and accept compares the guest's
email (trimmed, case-insensitive) before minting a token. Accept also no
longer creates the room: a guest can only join a call a human is already in.

2026-10-01 (feat/meetings-lobby, MC-3): ``new_guest_identity`` and
``issue_guest_token`` factored out of accept so the meeting lobby
(meetings/lobby_service.py) mints guest tokens the same way: ``guest-<hex16>``
identity, room-scoped grant, 1 hour.
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud.models.invite import MeetingInvite as _MeetingInviteDoc
from pocketpaw_ee.cloud.models.invite import hash_token as _hash_token

logger = logging.getLogger(__name__)

# Maximum lifetime for a meeting invite.
MAX_INVITE_TTL_HOURS = 72


def _validate_ttl_hours(ttl_hours: int) -> int:
    """Clamp TTL to the permitted range and return the effective value."""
    if ttl_hours < 1:
        return 1
    if ttl_hours > MAX_INVITE_TTL_HOURS:
        return MAX_INVITE_TTL_HOURS
    return ttl_hours


def _split_emails(display_name: str) -> tuple[str, list[str]]:
    """Pull an ``emails`` list out of a JSON ``display_name``.

    Returns the display name without ``emails`` and the normalized list. A
    plain-text name, or JSON with no ``emails`` key, comes back unchanged.
    """
    try:
        meta = json.loads(display_name)
    except (TypeError, ValueError):
        return display_name, []
    if not isinstance(meta, dict) or "emails" not in meta:
        return display_name, []
    raw = meta.pop("emails")
    emails = [
        e.strip().lower() for e in (raw if isinstance(raw, list) else []) if isinstance(e, str)
    ]
    return json.dumps(meta), [e for e in emails if e]


def _allowed_emails(doc: _MeetingInviteDoc) -> list[str]:
    """The invite's allow-list; legacy rows still carry it in ``display_name``."""
    return doc.allowed_emails or _split_emails(doc.display_name)[1]


def new_guest_identity() -> str:
    """``guest-<16 hex>``. Member identities are user ids (ObjectId hex) and the
    bot is ``call-bot``, so the prefix keeps guests from ever colliding."""
    return f"guest-{secrets.token_hex(8)}"


async def issue_guest_token(room_name: str, identity: str, display_name: str) -> str:
    """A 1-hour LiveKit token that joins ``room_name`` only.

    Callers must first make sure a human is in that room: LiveKit creates a
    missing room on connect, so a token for an empty room would start a call.
    """
    from pocketpaw_ee.cloud.livekit.service import generate_participant_token

    return await generate_participant_token(
        room_name=room_name,
        identity=identity,
        name=display_name,
        can_publish=True,
        can_subscribe=True,
        ttl_seconds=3600,  # 1 hour — typical meeting length
    )


async def create_meeting_invite(
    *,
    workspace_id: str,
    group_id: str,
    room_name: str,
    created_by: str,
    display_name: str = "",
    max_uses: int = 0,
    ttl_hours: int = 24,
) -> dict[str, Any]:
    """Create a shareable invite link for a LiveKit call.

    Returns a dict with ``invite_token`` (plaintext, for the URL) and
    ``invite_url``. The caller is responsible for building the full
    frontend URL.

    Raises ``ValidationError`` if the room name doesn't match the expected
    ``group-call-{group_id}`` pattern.
    """
    from pocketpaw_ee.cloud.livekit.service import room_name_for_group

    expected = room_name_for_group(group_id)
    if room_name != expected:
        raise ValidationError(
            "livekit.invalid_room",
            f"Room '{room_name}' does not belong to group '{group_id}'.",
        )

    display_name, allowed_emails = _split_emails(display_name)
    ttl = _validate_ttl_hours(ttl_hours)
    plaintext = secrets.token_urlsafe(32)
    token_hash = _hash_token(plaintext)

    doc = _MeetingInviteDoc(
        workspace=workspace_id,
        group_id=group_id,
        room_name=room_name,
        token_hash=token_hash,
        created_by=created_by,
        display_name=display_name,
        allowed_emails=allowed_emails,
        max_uses=max_uses,
        expires_at=datetime.now(UTC) + timedelta(hours=ttl),
    )
    await doc.insert()
    logger.info(
        "Created meeting invite for group %s (room %s) by user %s",
        group_id,
        room_name,
        created_by,
    )

    return {
        "invite_id": str(doc.id),
        "invite_token": plaintext,
        "group_id": group_id,
        "room_name": room_name,
        "created_by": created_by,
        "expires_at": doc.expires_at.isoformat(),
        "max_uses": max_uses,
    }


async def validate_meeting_invite(token: str) -> dict[str, Any]:
    """Validate an invite token and return room info for the join page.

    No authentication required — this is the public endpoint the guest
    hits when they open the invite link.

    Returns room metadata if the invite is valid. Raises ``NotFound`` if
    the token is unknown, ``Forbidden`` if expired/revoked/exhausted.
    """
    token_hash = _hash_token(token)
    doc = await _MeetingInviteDoc.find_one(
        _MeetingInviteDoc.token_hash == token_hash,
        _MeetingInviteDoc.revoked == False,  # noqa: E712
    )

    if doc is None:
        raise NotFound("meeting_invite", "Invite not found or has been revoked.")

    if doc.expired:
        raise Forbidden(
            "meeting_invite.expired",
            "This invite link has expired.",
        )

    if doc.exhausted:
        raise Forbidden(
            "meeting_invite.exhausted",
            "This invite link has reached its maximum number of uses.",
        )

    # Fetch the room info to check if the call is currently active. For a
    # scheduled meeting the room may not exist yet — the invite is still valid,
    # but join works only once a member has started the call.
    from pocketpaw_ee.cloud.livekit.service import get_room_info

    room_info = await get_room_info(doc.group_id)

    return {
        "valid": True,
        "room_name": doc.room_name,
        "group_id": doc.group_id,
        "workspace_id": doc.workspace,
        # Never the allow-list itself: anyone holding the link can call this.
        "display_name": _split_emails(doc.display_name)[0],
        "requires_email": bool(_allowed_emails(doc)),
        "is_call_active": room_info is not None and room_info.get("active", False),
        "participant_count": room_info.get("participant_count", 0) if room_info else 0,
        "expires_at": doc.expires_at.isoformat(),
        "max_uses": doc.max_uses,
        "use_count": doc.use_count,
    }


async def accept_meeting_invite(
    token: str,
    guest_display_name: str,
    email: str | None = None,
) -> dict[str, Any]:
    """Accept an invite and return a LiveKit guest token.

    The guest provides a ``guest_display_name`` that is shown to other
    participants. When the invite has an email allow-list, ``email`` must be
    on it (trimmed, case-insensitive) or the accept is refused. Returns a
    LiveKit access token so the guest can connect immediately.

    No authentication required — the guest may not have a Pocketpaw account.
    The returned token uses a ``guest-`` prefixed identity.

    Raises ``Forbidden`` if the invite is expired/revoked/exhausted or if
    the room is no longer active.
    """
    token_hash = _hash_token(token)
    doc = await _MeetingInviteDoc.find_one(
        _MeetingInviteDoc.token_hash == token_hash,
        _MeetingInviteDoc.revoked == False,  # noqa: E712
    )

    if doc is None:
        raise NotFound("meeting_invite", "Invite not found or has been revoked.")

    if doc.expired:
        raise Forbidden(
            "meeting_invite.expired",
            "This invite link has expired.",
        )

    if doc.exhausted:
        raise Forbidden(
            "meeting_invite.exhausted",
            "This invite link has reached its maximum number of uses.",
        )

    allowed = _allowed_emails(doc)
    if allowed and (email or "").strip().lower() not in allowed:
        raise Forbidden(
            "meeting_invite.email_not_allowed",
            "This email is not on the invite list. Check with the meeting host.",
        )

    # A guest link joins a running call; it never starts one. Until 2026-09-30
    # a missing room was created here with no workspace_id, which skipped the
    # plan's daily call budget and the Meeting insert, and let any unexpired
    # link restart an ended call. "Running" means a human is in the room (see
    # service.get_room_info), so a room left holding only the call-bot counts
    # as ended too.
    from pocketpaw_ee.cloud.livekit.service import LIVEKIT_URL, get_room_info

    room_info = await get_room_info(doc.group_id)
    if room_info is None or not room_info.get("active", False):
        raise Forbidden(
            "meeting_invite.call_ended",
            "This call isn't running. It may have ended, or the host hasn't started it yet.",
        )

    guest_id = new_guest_identity()
    lk_token = await issue_guest_token(doc.room_name, guest_id, guest_display_name)

    # Record the use.
    doc.use_count += 1
    doc.guest_identities.append(guest_id)
    await doc.save()

    logger.info(
        "Guest '%s' (%s) joined room %s via invite %s",
        guest_display_name,
        guest_id,
        doc.room_name,
        doc.id,
    )

    # Emit a participant-joined event so group members see the guest.
    try:
        from pocketpaw_ee.cloud.realtime.emit import emit
        from pocketpaw_ee.cloud.realtime.events import CallParticipantJoined

        await emit(
            CallParticipantJoined(
                data={
                    "group_id": doc.group_id,
                    "room_name": doc.room_name,
                    "identity": guest_id,
                    "name": guest_display_name,
                }
            )
        )
    except Exception:
        logger.debug("Failed to emit CallParticipantJoined for guest %s", guest_id)

    return {
        "token": lk_token,
        "url": LIVEKIT_URL,
        "room_name": doc.room_name,
        "identity": guest_id,
        "display_name": guest_display_name,
        "group_id": doc.group_id,
    }


async def list_meeting_invites(
    group_id: str,
) -> list[dict[str, Any]]:
    """List all active (non-revoked, non-expired) invites for a group."""
    now = datetime.now(UTC)
    docs = await _MeetingInviteDoc.find(
        _MeetingInviteDoc.group_id == group_id,
        _MeetingInviteDoc.revoked == False,  # noqa: E712
        _MeetingInviteDoc.expires_at > now,
    ).to_list()

    return [
        {
            "invite_id": str(d.id),
            "group_id": d.group_id,
            "room_name": d.room_name,
            "display_name": d.display_name,
            "max_uses": d.max_uses,
            "use_count": d.use_count,
            "guest_count": len(d.guest_identities),
            "expires_at": d.expires_at.isoformat(),
            "created_at": d.created_at.isoformat(),
            "created_by": d.created_by,
        }
        for d in docs
    ]


async def revoke_meeting_invite(
    invite_id: str,
    revoked_by: str,
    group_id: str,
) -> dict[str, Any]:
    """Revoke a meeting invite so it can no longer be used.

    ``group_id`` is the group the CALLER proved membership of, and the check
    below is why it is a required parameter. Until 2026-09-11 this function took
    only ``invite_id`` and fetched by primary key, so the route's membership
    check on the path ``{group_id}`` was decorative: satisfy it with a group you
    created yourself, then pass any invite id on the deployment. Three response
    classes made it sweepable rather than targeted — 404 for an unknown id, 200
    carrying the victim's ``group_id`` for a valid foreign one — and every hit
    set ``revoked=True`` permanently, so a sweep destroyed what it found.

    This is the shape ``/rooms/{group_id}/leave`` had until 2026-08-11 (see the
    module docstring in ``router.py``). It was fixed there and left standing
    here. ``create_meeting_invite`` cross-checks its room against the group at
    line 55; this is the same check on the other side.

    NotFound rather than Forbidden, and the SAME NotFound an unknown id raises,
    so the response cannot separate "exists elsewhere" from "does not exist".
    """
    from beanie import PydanticObjectId

    doc = await _MeetingInviteDoc.get(PydanticObjectId(invite_id))
    if doc is None or doc.group_id != group_id:
        raise NotFound("meeting_invite", invite_id)

    doc.revoked = True
    await doc.save()

    logger.info(
        "Revoked meeting invite %s for group %s by user %s",
        invite_id,
        doc.group_id,
        revoked_by,
    )

    return {
        "invite_id": str(doc.id),
        "group_id": doc.group_id,
        "revoked": True,
    }
