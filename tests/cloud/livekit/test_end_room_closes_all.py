# tests/cloud/livekit/test_end_room_closes_all.py — one room, two live rows.
#
# Created 2026-10-01 (feat/meetings-instant, MC-1). A scheduled start that lands
# on a running instant call (or the reverse) leaves two in_progress LiveKit
# Meeting rows for one room. ``end_room`` used to close only the first, leaving
# the other "Live" forever; it now closes all of them. The daily usage merges
# the two rows' overlapping spans so one call's time is counted once.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pocketpaw_ee.cloud.livekit import service
from pocketpaw_ee.cloud.models.meeting import Meeting

WS = "ws-1"
ROOM = "group-call-g1"


@pytest.fixture
def lk(monkeypatch):
    api = MagicMock()
    api.room.delete_room = AsyncMock()
    api.__aenter__ = AsyncMock(return_value=api)
    api.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(service, "LiveKitAPI", MagicMock(return_value=api))
    monkeypatch.setattr(service, "LIVEKIT_URL", "wss://t")
    monkeypatch.setattr(service, "LIVEKIT_API_KEY", "k")
    monkeypatch.setattr(service, "LIVEKIT_API_SECRET", "s")
    return api


async def _live_row(minutes_ago: float, title: str) -> Meeting:
    doc = Meeting(
        workspace=WS,
        source="livekit",
        provider_meeting_id=ROOM,
        title=title,
        join_url="",
        actual_start=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        status="in_progress",
    )
    await doc.insert()
    return doc


async def test_end_room_closes_every_in_progress_row_for_the_room(mongo_db, lk) -> None:
    await _live_row(10, "Instant call")
    await _live_row(5, "Standup")

    await service.end_room("g1", WS)

    rows = await Meeting.find_all().to_list()
    assert [r.status for r in rows] == ["ended", "ended"]
    assert all(r.actual_end is not None for r in rows)


async def test_two_rows_for_one_room_count_once(mongo_db) -> None:
    await _live_row(10, "Instant call")
    await _live_row(5, "Standup")

    used = await service._daily_call_usage(WS)

    assert 9 * 60 <= used <= 11 * 60  # the 10-minute call, not 15


async def test_separate_rooms_still_add_up(mongo_db) -> None:
    await _live_row(10, "Instant call")
    other = await _live_row(5, "Other room")
    other.provider_meeting_id = "group-call-g2"
    await other.save()

    used = await service._daily_call_usage(WS)

    assert 14 * 60 <= used <= 16 * 60
