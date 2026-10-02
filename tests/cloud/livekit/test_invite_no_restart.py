# tests/cloud/livekit/test_invite_no_restart.py — a guest link can't restart a call.
#
# Created 2026-09-30 (fix/livekit-call-security, MC-0 hole 3). Accepting an
# invite whose room no longer existed called ``create_room(group_id)`` with no
# workspace, which skipped the plan's daily call-budget gate and the Meeting
# insert. ``get_room_info`` also reported ``active: True`` for any room that
# existed, so accept's "call ended" branch could never run. Now accept refuses
# with ``meeting_invite.call_ended`` unless a human is in the room, and
# ``active`` means exactly that.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.cloud.livekit import invites as invite_service
from pocketpaw_ee.cloud.models.invite import MeetingInvite as _MeetingInviteDoc
from pocketpaw_ee.cloud.shared.errors import Forbidden

pytestmark = pytest.mark.usefixtures("mongo_db")


async def test_accept_does_not_restart_an_ended_call(livekit_stub, create_invite) -> None:
    """The finding: no room -> create_room(group_id), no budget gate, no Meeting."""
    token = await create_invite()
    livekit_stub.info.return_value = None

    with pytest.raises(Forbidden) as exc:
        await invite_service.accept_meeting_invite(token, "Guest")

    assert exc.value.code == "meeting_invite.call_ended"
    livekit_stub.create.assert_not_awaited()
    doc = await _MeetingInviteDoc.find_one(_MeetingInviteDoc.group_id == "g1")
    assert doc.use_count == 0


async def test_accept_refuses_a_room_with_no_humans_left(livekit_stub, create_invite) -> None:
    token = await create_invite()
    livekit_stub.info.return_value = {
        "room_name": "group-call-g1",
        "group_id": "g1",
        "participant_count": 1,
        "participants": [{"identity": "call-bot", "name": "call-bot"}],
        "active": False,
    }

    with pytest.raises(Forbidden) as exc:
        await invite_service.accept_meeting_invite(token, "Guest")
    assert exc.value.code == "meeting_invite.call_ended"
    livekit_stub.create.assert_not_awaited()


async def test_get_room_info_active_means_a_human_is_in_the_room() -> None:
    """``active`` was hard-coded True for any existing room."""
    from pocketpaw_ee.cloud.livekit import service

    def _participant(identity: str):
        return SimpleNamespace(identity=identity, name=identity, joined_at=None, kind=0)

    room_svc = MagicMock()
    room_svc.list_rooms = AsyncMock(return_value=SimpleNamespace(rooms=[object()]))
    room_svc.list_participants = AsyncMock(
        return_value=SimpleNamespace(participants=[_participant("call-bot")])
    )
    lk = MagicMock()
    lk.room = room_svc
    lk.__aenter__ = AsyncMock(return_value=lk)
    lk.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.object(service, "LiveKitAPI", return_value=lk),
        patch.object(service, "LIVEKIT_URL", "wss://t"),
        patch.object(service, "LIVEKIT_API_KEY", "k"),
        patch.object(service, "LIVEKIT_API_SECRET", "s"),
    ):
        bot_only = await service.get_room_info("g1")
        room_svc.list_participants.return_value = SimpleNamespace(
            participants=[_participant("call-bot"), _participant("u1")]
        )
        with_human = await service.get_room_info("g1")

    assert bot_only["active"] is False
    assert with_human["active"] is True
