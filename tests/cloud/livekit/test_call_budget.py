# tests/cloud/livekit/test_call_budget.py — the daily LiveKit call-time budget
# gate (feat/billing-rbac-member-caps).
#
# ``livekit.service.create_room`` is the CALL-START seam: it resolves the
# workspace's plan (entitlements) and enforces ``max_call_seconds_per_day`` —
# Free (0) blocks every call at entry, a paid tier with an exhausted daily
# budget blocks creating a NEW room (but still allows joining an already-running
# one, which already owns its budget), each new call records its remaining
# budget as ``Meeting.call_budget_deadline`` and schedules ``_force_end_at_budget``
# so a single over-budget call is force-ended at its deadline.
#
# DB-backed (mongo_db): real Workspace + Meeting docs drive the budget usage
# sum. LiveKitAPI and the agent subprocess are mocked (no real HTTP / child
# process). Recording bus is autouse via tests/cloud/conftest.
#
# Created 2026-08-08 (feat/billing-rbac-member-caps).

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.cloud._core.errors import CallLimitError
from pocketpaw_ee.cloud.livekit import service
from pocketpaw_ee.cloud.models.meeting import Meeting as MeetingDoc
from pocketpaw_ee.cloud.models.workspace import Workspace


@pytest.fixture
def mock_lk_api():
    """Mock LiveKitAPI async context manager + LiveKit env vars.

    Defaults to NO existing room (the create path); a test that wants a join
    overrides ``list_rooms`` to return an existing room.
    """
    service._active_agents.clear()

    mock_room_svc = MagicMock()
    mock_api_instance = MagicMock()
    mock_api_instance.room = mock_room_svc
    mock_api_instance.__aenter__ = AsyncMock(return_value=mock_api_instance)
    mock_api_instance.__aexit__ = AsyncMock(return_value=False)

    patches = [
        patch("pocketpaw_ee.cloud.livekit.service.LiveKitAPI", return_value=mock_api_instance),
        patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_URL", "wss://test.livekit.cloud"),
        patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_KEY", "test-key"),
        patch("pocketpaw_ee.cloud.livekit.service.LIVEKIT_API_SECRET", "test-secret"),
    ]
    for p in patches:
        p.start()
    try:
        mock_room_svc.list_rooms = AsyncMock(return_value=MagicMock(rooms=[]))
        mock_room_svc.create_room = AsyncMock(return_value=MagicMock(name="group-call-g1"))
        yield mock_room_svc
    finally:
        for p in patches:
            p.stop()
        service._active_agents.clear()


async def _make_workspace(plan: str) -> str:
    ws = Workspace(name="Acme", slug="acme", owner="u-owner", plan=plan)
    await ws.insert()
    return str(ws.id)


async def _seed_ended_call(workspace_id: str, minutes: float) -> None:
    """Insert an ENDED LiveKit meeting today that spans ``minutes``."""
    now = datetime.now(UTC)
    await MeetingDoc(
        workspace=workspace_id,
        source="livekit",
        provider_meeting_id="group-call-prev",
        title="Instant call",
        join_url="",
        actual_start=now - timedelta(minutes=minutes),
        actual_end=now,
        status="ended",
    ).insert()


# ---------------------------------------------------------------------------
# Free — no calls at all
# ---------------------------------------------------------------------------


async def test_free_blocks_every_call(mongo_db, mock_lk_api) -> None:
    """Free (max_call_seconds_per_day == 0) cannot create a call at all."""
    ws_id = await _make_workspace("free")
    with pytest.raises(CallLimitError) as exc:
        await service.create_room("g1", ws_id, "u-owner")
    assert exc.value.status_code == 402
    assert exc.value.code == "billing.call_limit"
    mock_lk_api.create_room.assert_not_called()


# ---------------------------------------------------------------------------
# Paid tiers — the daily cumulative budget
# ---------------------------------------------------------------------------


async def test_paid_blocks_new_room_when_budget_exhausted(mongo_db, mock_lk_api) -> None:
    """Go (30 min/day) with 31 min already used today cannot start a NEW call."""
    ws_id = await _make_workspace("go")
    await _seed_ended_call(ws_id, minutes=31)
    with pytest.raises(CallLimitError):
        await service.create_room("g1", ws_id, "u-owner")
    mock_lk_api.create_room.assert_not_called()


async def test_paid_allows_new_room_within_budget_and_sets_deadline(mongo_db, mock_lk_api) -> None:
    """Go with budget remaining succeeds, records the deadline, schedules the watchdog."""
    ws_id = await _make_workspace("go")
    service._active_agents["g1"] = MagicMock()  # skip the agent subprocess spawn

    with patch.object(service, "_force_end_at_budget", new_callable=AsyncMock) as mock_force:
        result = await service.create_room("g1", ws_id, "u-owner")
        await asyncio.sleep(0)  # let the scheduled watchdog coroutine run

    assert result["is_new"] is True
    meeting = await MeetingDoc.find_one(
        MeetingDoc.workspace == ws_id,
        MeetingDoc.provider_meeting_id == "group-call-g1",
    )
    assert meeting is not None
    # Deadline ≈ now + the full 30-min Go budget (nothing used today).
    assert meeting.call_budget_deadline is not None
    deadline = service._as_utc(meeting.call_budget_deadline)
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    assert 29 * 60 <= remaining <= 31 * 60
    # The watchdog was scheduled with (group_id, workspace_id, deadline).
    # (The DB round-trip truncates microseconds, so compare within 1s.)
    mock_force.assert_awaited_once()
    assert mock_force.await_args[0][:2] == ("g1", ws_id)
    assert abs((mock_force.await_args[0][2] - deadline).total_seconds()) < 1


async def test_paid_remaining_budget_is_shrunk_by_usage(mongo_db, mock_lk_api) -> None:
    """10 min of Go's 30 already used today → deadline is 20 min away, not 30."""
    ws_id = await _make_workspace("go")
    await _seed_ended_call(ws_id, minutes=10)
    service._active_agents["g1"] = MagicMock()

    with patch.object(service, "_force_end_at_budget", new_callable=AsyncMock):
        await service.create_room("g1", ws_id, "u-owner")

    meeting = await MeetingDoc.find_one(
        MeetingDoc.workspace == ws_id,
        MeetingDoc.provider_meeting_id == "group-call-g1",
    )
    assert meeting is not None and meeting.call_budget_deadline is not None
    deadline = service._as_utc(meeting.call_budget_deadline)
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    assert 19 * 60 <= remaining <= 21 * 60


# ---------------------------------------------------------------------------
# Joins vs new rooms
# ---------------------------------------------------------------------------


async def test_join_existing_room_allowed_even_when_budget_exhausted(mongo_db, mock_lk_api) -> None:
    """An exhausted budget blocks NEW rooms only — joining a live call is fine."""
    ws_id = await _make_workspace("go")
    await _seed_ended_call(ws_id, minutes=31)
    service._active_agents["g1"] = MagicMock()

    mock_lk_api.list_rooms = AsyncMock(
        return_value=MagicMock(rooms=[MagicMock(name="group-call-g1")])
    )
    result = await service.create_room("g1", ws_id, "u-owner")
    assert result["is_new"] is False
    mock_lk_api.create_room.assert_not_called()


# ---------------------------------------------------------------------------
# Uncapped + no-workspace legacy paths
# ---------------------------------------------------------------------------


async def test_enterprise_is_uncapped_no_deadline(mongo_db, mock_lk_api) -> None:
    """Enterprise (None cap) creates the room and records no budget deadline."""
    ws_id = await _make_workspace("enterprise")
    service._active_agents["g1"] = MagicMock()

    result = await service.create_room("g1", ws_id, "u-owner")
    assert result["is_new"] is True
    meeting = await MeetingDoc.find_one(
        MeetingDoc.workspace == ws_id,
        MeetingDoc.provider_meeting_id == "group-call-g1",
    )
    assert meeting is not None
    assert meeting.call_budget_deadline is None


async def test_no_workspace_context_skips_the_gate(mongo_db, mock_lk_api) -> None:
    """A create_room call with no workspace context bypasses the gate entirely."""
    service._active_agents["g1"] = MagicMock()
    result = await service.create_room("g1")
    assert result["room_name"] == "group-call-g1"
    assert result["is_new"] is True


# ---------------------------------------------------------------------------
# Budget watchdog — force-end carries the reason so the UI can prompt
# ---------------------------------------------------------------------------


async def test_force_end_at_budget_emits_reason(mongo_db, mock_lk_api, recording_bus) -> None:
    """The budget watchdog force-end emits call.ended with reason='budget_exhausted'."""
    ws_id = await _make_workspace("go")
    room_name = service.room_name_for_group("g1")
    now = datetime.now(UTC)
    await MeetingDoc(
        workspace=ws_id,
        source="livekit",
        provider_meeting_id=room_name,
        title="Instant call",
        join_url="",
        actual_start=now,
        status="in_progress",
    ).insert()
    mock_lk_api.delete_room = AsyncMock()

    # Past deadline → the watchdog runs immediately.
    await service._force_end_at_budget("g1", ws_id, now - timedelta(seconds=1))

    mock_lk_api.delete_room.assert_awaited_once()
    ended = [e for e in recording_bus.events if e.type == "call.ended"]
    assert len(ended) >= 1
    assert ended[-1].data["reason"] == "budget_exhausted"
