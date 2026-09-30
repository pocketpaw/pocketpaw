# tests/cloud/meetings/test_meeting_by_code.py — meetings for later + join by code.
#
# Created 2026-10-01 (feat/meetings-by-code, MC-2). ``POST /meetings`` with
# ``source="livekit"`` and no ``group_id`` makes a hidden meeting room and a
# coded Meeting without starting a call. ``GET /meetings/by-code/{code}`` is a
# public, rate-limited lookup that returns six fields and nothing else.
# ``POST /meetings/by-code/{code}/join`` lets a member of the meeting's
# workspace into the room and makes sure the call is running (budget gate,
# one room, one Meeting row). LiveKitAPI and the call-bot are mocked; the
# LiveKit "server" keeps a list of rooms so a second join sees the first room.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import rate_limit
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.chat import group_service
from pocketpaw_ee.cloud.livekit import service as livekit_service
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.dto import CreateMeetingRequest, ListMeetingsRequest
from pocketpaw_ee.cloud.models.group import Group
from pocketpaw_ee.cloud.models.meeting import Meeting
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.models.workspace import Workspace

LOOKUP_KEYS = {"code", "title", "scheduled_start", "host_name", "access", "status"}


@pytest.fixture(autouse=True)
def _no_scheduler():
    import pocketpaw_ee.cloud.meetings.providers.livekit  # noqa: F401 — registers provider

    with patch("pocketpaw_ee.cloud.meetings.scheduling.reminders.schedule_meeting_jobs"):
        yield


@pytest.fixture(autouse=True)
def _fresh_lookup_limiter():
    rate_limit._meeting_lookup_limiter._buckets.clear()
    yield
    rate_limit._meeting_lookup_limiter._buckets.clear()


@pytest.fixture
def lk(monkeypatch):
    """LiveKit mocked with a stateful room list; no real call-bot spawn."""
    livekit_service._active_agents.clear()
    rooms: list[SimpleNamespace] = []
    participants: list[SimpleNamespace] = []

    async def _create(req):
        rooms.append(SimpleNamespace(name=req.name))

    async def _list(req):
        names = set(req.names or [])
        return MagicMock(rooms=[r for r in rooms if not names or r.name in names])

    room_svc = MagicMock()
    room_svc.create_room = AsyncMock(side_effect=_create)
    room_svc.list_rooms = AsyncMock(side_effect=_list)
    room_svc.list_participants = AsyncMock(
        side_effect=lambda req: MagicMock(participants=list(participants))
    )
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
    monkeypatch.setenv("POCKETPAW_FRONTEND_BASE_URL", "https://app.example.com")
    resolver = MagicMock()
    monkeypatch.setattr(group_service, "get_resolver", lambda: resolver)
    yield SimpleNamespace(svc=room_svc, rooms=rooms, participants=participants, resolver=resolver)
    livekit_service._active_agents.clear()


async def _host(name: str = "Hana Host") -> str:
    user = User(email=f"{name.split()[0].lower()}@x.dev", hashed_password="x", full_name=name)
    await user.insert()
    return str(user.id)


async def _workspace(plan: str, owner: str = "owner") -> str:
    ws = Workspace(name="Acme", slug=f"acme-{plan}-{owner}", owner=owner, plan=plan)
    await ws.insert()
    return str(ws.id)


async def _for_later(ws_id: str, host: str, **body) -> object:
    body = {"source": "livekit", "title": "Planning", **body}
    return await meetings_service.create_meeting(ws_id, host, CreateMeetingRequest(**body))


def _near(dt: datetime | None, target: datetime) -> bool:
    assert dt is not None
    return abs(meetings_service._aware(dt) - target) < timedelta(minutes=1)


# ---------------------------------------------------------------------------
# Create for later
# ---------------------------------------------------------------------------


async def test_for_later_creates_room_and_coded_meeting_without_a_call(
    mongo_db, lk, recording_bus
) -> None:
    ws_id = await _workspace("enterprise")
    host = await _host()

    out = await _for_later(ws_id, host)

    rooms = await Group.find_all().to_list()
    assert len(rooms) == 1
    room = rooms[0]
    assert (room.type, room.members, room.owner, room.name) == ("meeting", [host], host, "Planning")

    row = await Meeting.find_one()
    assert row.status == "scheduled"
    assert row.scheduled_start is None
    assert row.actual_start is None
    assert row.room_group_id == str(room.id)
    assert row.raw_provider_payload["group_id"] == str(room.id)
    assert (row.host_user_id, row.access) == (host, "ask")
    assert _near(row.link_expires_at, datetime.now(UTC) + timedelta(days=30))

    assert out.code and out.link == f"https://app.example.com/m/{out.code}"
    assert out.join_url == out.link
    assert out.room_group_id == str(room.id)

    lk.svc.create_room.assert_not_called()
    assert not [e for e in recording_bus.events if e.type == "meeting.started"]


async def test_dated_for_later_link_lasts_until_the_meeting_ends(mongo_db, lk) -> None:
    ws_id = await _workspace("enterprise")
    host = await _host()
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)

    await _for_later(ws_id, host, scheduled_start=start, duration_minutes=45)

    row = await Meeting.find_one()
    assert row.status == "scheduled"
    assert _near(row.scheduled_start, start)
    assert row.link_expires_at == row.scheduled_end
    assert _near(row.link_expires_at, start + timedelta(minutes=45))


async def test_meeting_on_a_chat_room_keeps_its_room_and_gets_a_code(mongo_db, lk) -> None:
    ws_id = await _workspace("enterprise")
    chat = Group(workspace=ws_id, name="Team", owner="u1", members=["u1", "u2"])
    await chat.insert()

    out = await _for_later(ws_id, "u1", group_id=str(chat.id))

    assert await Group.find_all().count() == 1  # no hidden room made
    row = await Meeting.find_one()
    assert row.raw_provider_payload["group_id"] == str(chat.id)
    assert row.room_group_id is None
    assert row.join_url == ""  # unchanged from before codes
    assert out.code and out.link.endswith(f"/m/{out.code}")
    assert out.group_id == str(chat.id)


async def test_recall_meetings_get_no_code(mongo_db) -> None:
    fake = SimpleNamespace(
        create=AsyncMock(
            return_value=SimpleNamespace(provider_payload={"id": "z-1"}, join_url="https://z")
        )
    )
    with patch("pocketpaw_ee.cloud.meetings.providers.base.resolve", return_value=fake):
        out = await meetings_service.create_meeting(
            "ws", "u1", CreateMeetingRequest(source="recall", provider="zoom", title="Ext")
        )
    assert out.code is None and out.link is None


async def test_expired_undated_meeting_self_heals_to_ended(mongo_db, lk) -> None:
    ws_id = await _workspace("enterprise")
    host = await _host()
    await _for_later(ws_id, host)
    row = await Meeting.find_one()
    row.link_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await row.save()

    listed = await meetings_service.list_meetings(ws_id, ListMeetingsRequest())

    assert [m.status for m in listed] == ["ended"]


async def test_join_group_refuses_a_meeting_room(mongo_db, lk) -> None:
    ws_id = await _workspace("enterprise")
    host = await _host()
    out = await _for_later(ws_id, host)

    with pytest.raises(Forbidden):
        await group_service.join_group(out.room_group_id, "u-other", ws_id)


# ---------------------------------------------------------------------------
# HTTP — lookup + join
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(monkeypatch, mongo_db):  # noqa: ARG001 — mongo_db forces Beanie init
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.meetings.router import router
    from pocketpaw_ee.guards import deps as guards_deps

    monkeypatch.setattr(guards_deps, "check_workspace_action", AsyncMock(return_value=None))
    state = SimpleNamespace(user=None)

    async def _user():
        if state.user is None:
            from fastapi import HTTPException

            raise HTTPException(401, "Unauthorized")
        return state.user

    def act_as(user_id: str, ws_id: str) -> None:
        state.user = SimpleNamespace(
            id=user_id,
            active_workspace=ws_id,
            workspaces=[SimpleNamespace(workspace=ws_id, role="member")],
        )

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = _user
    app.include_router(router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.act_as = act_as
        c.log_out = lambda: setattr(state, "user", None)
        yield c


async def _meeting(ws_id: str, host: str, **body) -> object:
    return await _for_later(ws_id, host, **body)


async def test_lookup_returns_only_the_public_fields(client, lk) -> None:
    ws_id = await _workspace("enterprise")
    host = await _host("Hana Host")
    out = await _meeting(ws_id, host)

    resp = await client.get(f"/api/v1/meetings/by-code/{out.code}")

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == LOOKUP_KEYS
    assert body == {
        "code": out.code,
        "title": "Planning",
        "scheduled_start": None,
        "host_name": "Hana Host",
        "access": "ask",
        "status": "not_started",
    }


@pytest.mark.parametrize("form", ["dashed", "undashed", "upper"])
async def test_lookup_accepts_every_code_form(client, lk, form) -> None:
    ws_id = await _workspace("enterprise")
    out = await _meeting(ws_id, await _host())
    code = {
        "dashed": out.code,
        "undashed": out.code.replace("-", ""),
        "upper": out.code.upper(),
    }[form]

    resp = await client.get(f"/api/v1/meetings/by-code/{code}")

    assert resp.status_code == 200
    assert resp.json()["code"] == out.code


@pytest.mark.parametrize("code", ["abc-defg-hjk", "not-a-code", "abcdefghjkm", "lll-llll-lll"])
async def test_lookup_unknown_code_is_404(client, lk, code) -> None:
    resp = await client.get(f"/api/v1/meetings/by-code/{code}")
    assert resp.status_code == 404


async def test_lookup_reports_live_when_someone_is_in_the_room(client, lk) -> None:
    ws_id = await _workspace("enterprise")
    out = await _meeting(ws_id, await _host())
    lk.rooms.append(SimpleNamespace(name=f"group-call-{out.room_group_id}"))
    lk.participants.extend([SimpleNamespace(identity="call-bot", name="", joined_at=0, kind=0)])

    resp = await client.get(f"/api/v1/meetings/by-code/{out.code}")
    assert resp.json()["status"] == "not_started"  # only the bot

    lk.participants.append(SimpleNamespace(identity="u1", name="U", joined_at=0, kind=0))
    resp = await client.get(f"/api/v1/meetings/by-code/{out.code}")
    assert resp.json()["status"] == "live"


@pytest.mark.parametrize("how", ["ended", "cancelled", "expired"])
async def test_lookup_reports_ended(client, lk, how) -> None:
    ws_id = await _workspace("enterprise")
    out = await _meeting(ws_id, await _host())
    row = await Meeting.find_one()
    if how == "expired":
        row.link_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        row.status = how
    await row.save()

    resp = await client.get(f"/api/v1/meetings/by-code/{out.code}")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ended"


async def test_lookup_needs_no_login_and_is_rate_limited(client, lk) -> None:
    ws_id = await _workspace("enterprise")
    out = await _meeting(ws_id, await _host())
    client.log_out()
    url = f"/api/v1/meetings/by-code/{out.code}"
    capacity = rate_limit._meeting_lookup_limiter.capacity

    for _ in range(capacity):
        assert (
            await client.get(url, headers={"x-forwarded-for": "203.0.113.7"})
        ).status_code == 200
    blocked = await client.get(url, headers={"x-forwarded-for": "203.0.113.7"})
    other_ip = await client.get(url, headers={"x-forwarded-for": "203.0.113.8"})

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "meetings.lookup_rate_limited"
    assert other_ip.status_code == 200


async def test_members_join_by_code_and_the_call_starts_once(client, lk, recording_bus) -> None:
    ws_id = await _workspace("enterprise")
    host = await _host()
    out = await _meeting(ws_id, host)
    room_name = f"group-call-{out.room_group_id}"

    client.act_as("u-ann", ws_id)
    first = await client.post(f"/api/v1/meetings/by-code/{out.code.upper()}/join")
    client.act_as("u-bob", ws_id)
    second = await client.post(f"/api/v1/meetings/by-code/{out.code.replace('-', '')}/join")

    assert first.status_code == 200, first.text
    assert first.json() == {"room_group_id": out.room_group_id, "room_name": room_name}
    assert second.json() == first.json()

    room = await Group.get(out.room_group_id)
    assert room.members == [host, "u-ann", "u-bob"]
    assert lk.resolver.invalidate_group.call_count == 2  # cached audiences refresh
    assert lk.svc.create_room.await_count == 1
    assert await Meeting.find_all().count() == 1
    assert await Group.find_all().count() == 1

    row = await Meeting.find_one()
    assert row.status == "in_progress"
    assert row.actual_start is not None
    assert row.provider_meeting_id == room_name

    started = [e for e in recording_bus.events if e.type == "meeting.started"]
    assert len(started) == 1
    assert started[0].data["group_id"] == out.room_group_id


async def test_a_member_already_in_the_room_is_not_added_twice(client, lk) -> None:
    ws_id = await _workspace("enterprise")
    host = await _host()
    out = await _meeting(ws_id, host)

    client.act_as(host, ws_id)
    resp = await client.post(f"/api/v1/meetings/by-code/{out.code}/join")

    assert resp.status_code == 200
    assert (await Group.get(out.room_group_id)).members == [host]


async def test_join_restarts_a_call_whose_room_closed(client, lk, recording_bus) -> None:
    ws_id = await _workspace("enterprise")
    out = await _meeting(ws_id, await _host())
    client.act_as("u-ann", ws_id)
    await client.post(f"/api/v1/meetings/by-code/{out.code}/join")
    first_start = (await Meeting.find_one()).actual_start
    lk.rooms.clear()  # everyone left; LiveKit's empty timeout closed the room

    resp = await client.post(f"/api/v1/meetings/by-code/{out.code}/join")

    assert resp.status_code == 200
    assert lk.svc.create_room.await_count == 2
    row = await Meeting.find_one()
    assert row.status == "in_progress"
    assert row.actual_start == first_start
    assert await Meeting.find_all().count() == 1
    assert len([e for e in recording_bus.events if e.type == "meeting.started"]) == 2


async def test_join_from_another_workspace_is_403(client, lk) -> None:
    ws_id = await _workspace("enterprise")
    other_ws = await _workspace("enterprise", owner="someone-else")
    out = await _meeting(ws_id, await _host())

    client.act_as("u-out", other_ws)
    resp = await client.post(f"/api/v1/meetings/by-code/{out.code}/join")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "livekit.room_forbidden"
    assert "u-out" not in (await Group.get(out.room_group_id)).members
    lk.svc.create_room.assert_not_called()


async def test_join_without_call_budget_is_402_and_grants_nothing(client, lk) -> None:
    ws_id = await _workspace("free")
    out = await _meeting(ws_id, await _host())

    client.act_as("u-ann", ws_id)
    resp = await client.post(f"/api/v1/meetings/by-code/{out.code}/join")

    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "billing.call_limit"
    assert "u-ann" not in (await Group.get(out.room_group_id)).members
    assert (await Meeting.find_one()).status == "scheduled"


@pytest.mark.parametrize("how", ["ended", "cancelled", "expired"])
async def test_join_an_ended_meeting_is_410(client, lk, how) -> None:
    ws_id = await _workspace("enterprise")
    out = await _meeting(ws_id, await _host())
    row = await Meeting.find_one()
    if how == "expired":
        row.link_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        row.status = how
    await row.save()

    client.act_as("u-ann", ws_id)
    resp = await client.post(f"/api/v1/meetings/by-code/{out.code}/join")

    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "meeting.ended"
    lk.svc.create_room.assert_not_called()


async def test_join_unknown_code_is_404(client, lk) -> None:
    client.act_as("u-ann", "ws")
    resp = await client.post("/api/v1/meetings/by-code/abc-defg-hjk/join")
    assert resp.status_code == 404


async def test_join_needs_login(client, lk) -> None:
    ws_id = await _workspace("enterprise")
    out = await _meeting(ws_id, await _host())
    client.log_out()

    resp = await client.post(f"/api/v1/meetings/by-code/{out.code}/join")

    assert resp.status_code == 401


async def test_join_pushes_the_link_expiry_out(client, lk) -> None:
    ws_id = await _workspace("enterprise")
    out = await _meeting(ws_id, await _host())
    row = await Meeting.find_one()
    row.link_expires_at = datetime.now(UTC) + timedelta(days=2)
    await row.save()

    client.act_as("u-ann", ws_id)
    await client.post(f"/api/v1/meetings/by-code/{out.code}/join")

    row = await Meeting.find_one()
    assert _near(row.link_expires_at, datetime.now(UTC) + timedelta(days=30))


async def test_join_does_not_stretch_a_dated_meeting(client, lk) -> None:
    ws_id = await _workspace("enterprise")
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
    out = await _meeting(ws_id, await _host(), scheduled_start=start, duration_minutes=30)

    client.act_as("u-ann", ws_id)
    resp = await client.post(f"/api/v1/meetings/by-code/{out.code}/join")

    assert resp.status_code == 200
    row = await Meeting.find_one()
    assert row.link_expires_at == row.scheduled_end
    assert row.status == "in_progress"


async def test_join_by_code_never_grants_a_chat_room(client, lk) -> None:
    ws_id = await _workspace("enterprise")
    chat = Group(workspace=ws_id, name="Private", owner="u1", members=["u1"], type="private")
    await chat.insert()
    out = await _for_later(ws_id, "u1", group_id=str(chat.id))

    client.act_as("u-snoop", ws_id)
    denied = await client.post(f"/api/v1/meetings/by-code/{out.code}/join")
    client.act_as("u1", ws_id)
    allowed = await client.post(f"/api/v1/meetings/by-code/{out.code}/join")

    assert denied.status_code == 403
    assert (await Group.get(str(chat.id))).members == ["u1"]
    assert allowed.status_code == 200
    assert allowed.json()["room_group_id"] == str(chat.id)


async def test_add_meeting_room_member_refuses_other_rooms(mongo_db, lk) -> None:
    chat = Group(workspace="ws", name="Team", owner="u1", members=["u1"], type="private")
    await chat.insert()

    with pytest.raises(Forbidden):
        await group_service.add_meeting_room_member(str(chat.id), "u2")

    assert (await Group.get(str(chat.id))).members == ["u1"]
