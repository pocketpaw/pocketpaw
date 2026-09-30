# tests/cloud/meetings/conftest.py — shared LiveKit + HTTP fixtures for the
# meeting-code suites.
#
# Created 2026-10-01 (feat/meetings-lobby, MC-3): ``lk`` (LiveKitAPI mocked with a
# stateful room + participant list, no call-bot spawn) and ``client`` (the
# meetings + livekit routers on a bare FastAPI app with a switchable signed-in
# user) moved here from test_meeting_by_code.py so the lobby suite shares them.
# A test module that defines its own ``lk`` (test_instant_meeting.py) still wins.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.chat import group_service
from pocketpaw_ee.cloud.livekit import service as livekit_service


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


@pytest_asyncio.fixture
async def client(monkeypatch, mongo_db):  # noqa: ARG001 — mongo_db forces Beanie init
    import importlib

    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.livekit.router import router as livekit_router
    from pocketpaw_ee.cloud.meetings.router import router
    from pocketpaw_ee.guards import deps as guards_deps

    livekit_router_mod = importlib.import_module("pocketpaw_ee.cloud.livekit.router")
    monkeypatch.setattr(livekit_router_mod, "require_license", AsyncMock())

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
            full_name=user_id,
            active_workspace=ws_id,
            workspaces=[SimpleNamespace(workspace=ws_id, role="member")],
        )

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = _user
    app.include_router(router, prefix="/api/v1")
    app.include_router(livekit_router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.act_as = act_as
        c.log_out = lambda: setattr(state, "user", None)
        yield c
