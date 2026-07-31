"""Meeting invite service — shareable links for guest access to LiveKit calls.

Guests receive a temporary LiveKit access token with ``guest-`` prefixed
identity, bypassing the group membership check that guards regular
participant tokens. The plaintext invite token lives only in the shared URL;
we persist ``sha256(plaintext)`` so a DB read cannot reconstruct a usable link.
"""

from __future__ import annotations

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

    # Fetch the room info to confirm the call is still active.
    from pocketpaw_ee.cloud.livekit.service import get_room_info

    room_info = await get_room_info(doc.group_id)

    return {
        "valid": True,
        "room_name": doc.room_name,
        "group_id": doc.group_id,
        "workspace_id": doc.workspace,
        "display_name": doc.display_name,
        "is_call_active": room_info is not None and room_info.get("active", False),
        "participant_count": room_info.get("participant_count", 0) if room_info else 0,
        "expires_at": doc.expires_at.isoformat(),
        "max_uses": doc.max_uses,
        "use_count": doc.use_count,
    }


async def accept_meeting_invite(
    token: str,
    guest_display_name: str,
) -> dict[str, Any]:
    """Accept an invite and return a LiveKit guest token.

    The guest provides a ``guest_display_name`` that is shown to other
    participants. Returns a LiveKit access token so the guest can connect
    immediately.

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

    # Verify the call is still active.
    from pocketpaw_ee.cloud.livekit.service import LIVEKIT_URL, get_room_info

    room_info = await get_room_info(doc.group_id)
    if room_info is None or not room_info.get("active", False):
        raise Forbidden(
            "meeting_invite.call_ended",
            "The call has ended. This invite is no longer valid.",
        )

    # Generate a guest identity.
    guest_id = f"guest-{secrets.token_hex(8)}"

    # Generate a LiveKit access token for the guest.
    from pocketpaw_ee.cloud.livekit.service import generate_participant_token

    lk_token = await generate_participant_token(
        room_name=doc.room_name,
        identity=guest_id,
        name=guest_display_name,
        can_publish=True,
        can_subscribe=True,
        ttl_seconds=3600,  # 1 hour — typical meeting length
    )

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
) -> dict[str, Any]:
    """Revoke a meeting invite so it can no longer be used."""
    from beanie import PydanticObjectId

    doc = await _MeetingInviteDoc.get(PydanticObjectId(invite_id))
    if doc is None:
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
