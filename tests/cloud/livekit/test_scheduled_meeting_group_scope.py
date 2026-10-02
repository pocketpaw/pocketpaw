# tests/cloud/livekit/test_scheduled_meeting_group_scope.py — who can schedule a call.
#
# Created 2026-09-30 (fix/livekit-call-security, review S5). ``create_meeting``
# with ``source="livekit"`` stored whatever ``group_id`` it was given. A user
# could schedule a meeting on a group in another workspace, and when that
# meeting's end time came, ``end_room(group_id)`` would end the call running
# in that group. The LiveKit provider now requires the group to be in the
# caller's workspace with the caller as a member.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.dto import CreateMeetingRequest
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.models.meeting import Meeting as MeetingDoc

pytestmark = pytest.mark.usefixtures("mongo_db")

WS = "ws-mine"


@pytest.fixture(autouse=True)
def _no_scheduler():
    import pocketpaw_ee.cloud.meetings.providers.livekit  # noqa: F401 — registers provider

    with patch("pocketpaw_ee.cloud.meetings.scheduling.reminders.schedule_meeting_jobs"):
        yield


async def _group(workspace: str, members: list[str]) -> str:
    doc = _GroupDoc(workspace=workspace, name="Room", owner=members[0])
    doc.members = list(members)
    await doc.insert()
    return str(doc.id)


def _body(group_id: str) -> CreateMeetingRequest:
    return CreateMeetingRequest(
        source="livekit",
        group_id=group_id,
        title="Standup",
        scheduled_start=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.mark.parametrize(
    "where",
    ["other_workspace", "not_member", "unknown"],
)
async def test_cannot_schedule_a_call_on_someone_elses_group(where) -> None:
    if where == "other_workspace":
        gid = await _group("ws-other", ["u1"])
    elif where == "not_member":
        gid = await _group(WS, ["u2"])
    else:
        gid = "507f1f77bcf86cd799439011"

    with pytest.raises(Forbidden):
        await meetings_service.create_meeting(WS, "u1", _body(gid))

    assert await MeetingDoc.find(MeetingDoc.workspace == WS).count() == 0


async def test_a_member_can_schedule_a_call_on_their_group() -> None:
    gid = await _group(WS, ["u1", "u2"])

    await meetings_service.create_meeting(WS, "u1", _body(gid))

    rows = await MeetingDoc.find(MeetingDoc.workspace == WS).to_list()
    assert len(rows) == 1
    assert rows[0].raw_provider_payload["group_id"] == gid
