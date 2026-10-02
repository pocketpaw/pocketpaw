# tests/cloud/livekit/conftest.py — shared guest-invite fixtures.
#
# Created 2026-09-30 (fix/livekit-call-security, MC-0): ``livekit_stub`` fakes
# the LiveKit side of invite validate/accept, ``create_invite`` mints a real
# MeetingInvite in the per-test mongo DB.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def live_room(identities: list[str]) -> dict:
    """A get_room_info() payload for group g1 with these participants."""
    return {
        "room_name": "group-call-g1",
        "group_id": "g1",
        "participant_count": len(identities),
        "participants": [{"identity": i, "name": i} for i in identities],
        "active": any(i != "call-bot" for i in identities),
    }


@pytest.fixture
def livekit_stub():
    """A live room with a human in it; create_room is a spy."""
    with (
        patch(
            "pocketpaw_ee.cloud.livekit.service.get_room_info",
            new_callable=AsyncMock,
            return_value=live_room(["call-bot", "u1"]),
        ) as info,
        patch(
            "pocketpaw_ee.cloud.livekit.service.generate_participant_token",
            new_callable=AsyncMock,
            return_value="guest-lk-token",
        ),
        patch("pocketpaw_ee.cloud.livekit.service.create_room", new_callable=AsyncMock) as create,
    ):
        yield SimpleNamespace(info=info, create=create)


@pytest.fixture
def create_invite(mongo_db):  # noqa: ARG001 — fixture wires Beanie
    from pocketpaw_ee.cloud.livekit import invites as invite_service

    async def _create(display_name: str = "", max_uses: int = 0) -> str:
        result = await invite_service.create_meeting_invite(
            workspace_id="ws-mine",
            group_id="g1",
            room_name="group-call-g1",
            created_by="u1",
            display_name=display_name,
            max_uses=max_uses,
        )
        return result["invite_token"]

    return _create
