# tests/cloud/meetings/test_instant_meeting.py — POST /meetings/instant.
#
# Created 2026-10-01 (feat/meetings-instant, MC-1). Starting an instant meeting
# makes exactly one hidden ``type="meeting"`` room and exactly one Meeting row
# (with a unique, well-formed code) and starts the call through
# ``livekit.service.create_room`` so the daily call budget applies. A budget
# refusal leaves nothing behind. LiveKitAPI and the call-bot subprocess are
# mocked; Workspace + Meeting + Group are real mongomock rows.

from __future__ import annotations

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.errors import CallLimitError
from pocketpaw_ee.cloud.livekit import service as livekit_service
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.dto import StartInstantMeetingRequest
from pocketpaw_ee.cloud.models.group import Group
from pocketpaw_ee.cloud.models.meeting import Meeting
from pocketpaw_ee.cloud.models.workspace import Workspace
from pymongo.errors import DuplicateKeyError

HOST = "u-host"
CODE_RE = re.compile(r"^[a-hjkmnp-z]{10}$")
DISPLAY_RE = re.compile(r"^[a-hjkmnp-z]{3}-[a-hjkmnp-z]{4}-[a-hjkmnp-z]{3}$")


@pytest.fixture
def lk(monkeypatch):
    """LiveKit env + API mocked, no room exists yet, no real call-bot spawn."""
    livekit_service._active_agents.clear()
    room_svc = MagicMock()
    room_svc.list_rooms = AsyncMock(return_value=MagicMock(rooms=[]))
    room_svc.create_room = AsyncMock()
    api = MagicMock()
    api.room = room_svc
    api.__aenter__ = AsyncMock(return_value=api)
    api.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(livekit_service, "LiveKitAPI", MagicMock(return_value=api))
    monkeypatch.setattr(livekit_service, "LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setattr(livekit_service, "LIVEKIT_API_KEY", "k")
    monkeypatch.setattr(livekit_service, "LIVEKIT_API_SECRET", "s")
    monkeypatch.setattr(
        livekit_service, "_spawn_agent_process", AsyncMock(return_value=MagicMock())
    )
    monkeypatch.setattr(livekit_service, "_reap_agent_process", AsyncMock())
    monkeypatch.setattr(livekit_service, "_force_end_at_budget", AsyncMock())
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", "https://app.example.com/")
    yield room_svc
    livekit_service._active_agents.clear()


async def _workspace(plan: str) -> str:
    ws = Workspace(name="Acme", slug="acme", owner=HOST, plan=plan)
    await ws.insert()
    return str(ws.id)


async def _start(ws_id: str, **body) -> object:
    return await meetings_service.start_instant_meeting(
        ws_id, HOST, StartInstantMeetingRequest(**body)
    )


async def test_instant_creates_one_hidden_room_and_one_meeting(mongo_db, lk, recording_bus) -> None:
    ws_id = await _workspace("enterprise")

    out = await _start(ws_id, title="Design sync", description="Q4 plan")

    rooms = await Group.find_all().to_list()
    assert len(rooms) == 1
    room = rooms[0]
    assert (room.type, room.name, room.members, room.owner) == (
        "meeting",
        "Design sync",
        [HOST],
        HOST,
    )
    room_id = str(room.id)

    rows = await Meeting.find_all().to_list()
    assert len(rows) == 1  # create_room did not add an "Instant call" twin
    row = rows[0]
    assert CODE_RE.match(row.code)
    assert row.source == "livekit"
    assert row.status == "in_progress"
    assert row.room_group_id == room_id
    assert row.raw_provider_payload["group_id"] == room_id
    assert row.provider_meeting_id == f"group-call-{room_id}"
    assert (row.host_user_id, row.created_by_user_id, row.access) == (HOST, HOST, "ask")
    assert row.description == "Q4 plan"
    assert row.actual_start is not None

    assert DISPLAY_RE.match(out.code)
    assert out.code.replace("-", "") == row.code
    assert out.link == f"https://app.example.com/m/{out.code}"
    assert out.join_url == out.link
    assert (out.id, out.room_group_id, out.group_id) == (str(row.id), room_id, room_id)
    assert (out.host_user_id, out.access, out.title) == (HOST, "ask", "Design sync")

    lk.create_room.assert_awaited_once()
    started = [e for e in recording_bus.events if e.type == "meeting.started"]
    assert len(started) == 1
    assert started[0].data["group_id"] == room_id


async def test_instant_title_defaults(mongo_db, lk) -> None:
    ws_id = await _workspace("enterprise")

    out = await _start(ws_id)

    assert out.title == "Instant meeting"
    assert out.description is None


async def test_instant_records_the_budget_deadline(mongo_db, lk) -> None:
    ws_id = await _workspace("go")

    await _start(ws_id)

    row = await Meeting.find_one()
    assert row.call_budget_deadline is not None


async def test_budget_blocked_instant_creates_nothing(mongo_db, lk) -> None:
    ws_id = await _workspace("free")

    with pytest.raises(CallLimitError):
        await _start(ws_id, title="Blocked")

    assert await Group.find_all().count() == 0
    assert await Meeting.find_all().count() == 0
    lk.create_room.assert_not_called()


async def test_codes_retry_past_a_collision(mongo_db, lk) -> None:
    ws_id = await _workspace("enterprise")
    codes = iter(["aaaaaaaaaa", "aaaaaaaaaa", "bbbbbbbbbb"])

    with patch.object(meetings_service, "_new_code", side_effect=lambda: next(codes)):
        first = await _start(ws_id)
        second = await _start(ws_id)

    assert first.code == "aaa-aaaa-aaa"
    assert second.code == "bbb-bbbb-bbb"
    assert await Meeting.find_all().count() == 2


async def test_code_exhaustion_rolls_back(mongo_db, lk) -> None:
    ws_id = await _workspace("enterprise")

    with patch.object(meetings_service, "_new_code", return_value="aaaaaaaaaa"):
        await _start(ws_id)
        with pytest.raises(DuplicateKeyError):
            await _start(ws_id)

    assert await Group.find_all().count() == 1
    assert await Meeting.find_all().count() == 1


def test_new_code_uses_the_unambiguous_alphabet() -> None:
    for _ in range(200):
        assert CODE_RE.match(meetings_service._new_code())


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(monkeypatch, mongo_db):  # noqa: ARG001 — mongo_db forces Beanie init
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.meetings.router import router
    from pocketpaw_ee.guards import deps as guards_deps

    monkeypatch.setattr(guards_deps, "check_workspace_action", AsyncMock(return_value=None))
    ws_id = await _workspace("free")
    user = SimpleNamespace(
        id=HOST, active_workspace=ws_id, workspaces=[SimpleNamespace(workspace=ws_id, role="owner")]
    )

    async def _user():
        return user

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = _user
    app.include_router(router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_route_returns_402_on_free_and_creates_nothing(client, lk) -> None:
    resp = await client.post("/api/v1/meetings/instant", json={"title": "Hi"})

    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "billing.call_limit"
    assert await Group.find_all().count() == 0
    assert await Meeting.find_all().count() == 0


async def test_route_returns_the_meeting(client, lk, monkeypatch) -> None:
    monkeypatch.setattr(
        livekit_service, "_call_budget_remaining", AsyncMock(return_value=(None, 0))
    )

    resp = await client.post("/api/v1/meetings/instant", json={})

    assert resp.status_code == 200
    body = resp.json()
    assert DISPLAY_RE.match(body["code"])
    assert body["link"].endswith(f"/m/{body['code']}")
    assert body["status"] == "in_progress"
    assert body["source"] == "livekit"
    assert body["room_group_id"] and body["host_user_id"] == HOST
    assert body["access"] == "ask"
    assert "guest_emails" not in body
