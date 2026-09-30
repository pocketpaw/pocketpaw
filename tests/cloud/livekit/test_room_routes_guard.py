# tests/cloud/livekit/test_room_routes_guard.py — group-scoped call routes.
#
# Created 2026-09-30 (fix/livekit-call-security, MC-0 review fixes).
#
# S4: POST /rooms, GET/DELETE /rooms/{id} and the recording routes checked
#     group membership but not that the group is in the caller's active
#     workspace, so budget use and Meeting rows could land in the wrong
#     workspace. They now refuse with the same 403 as /token.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.livekit import service as livekit_service
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.models.workspace import Workspace

pytestmark = pytest.mark.usefixtures("mongo_db")


@pytest_asyncio.fixture
async def workspace_id(mongo_db) -> str:  # noqa: ARG001 — fixture wires Beanie
    ws = Workspace(name="Mine", slug="mine", owner="u1", plan="pro")
    await ws.insert()
    return str(ws.id)


@pytest_asyncio.fixture
async def client(workspace_id) -> AsyncClient:
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.deps import current_user, current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.livekit.router import router as livekit_router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(livekit_router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id="u1", full_name="User One")
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    with patch("pocketpaw_ee.cloud.livekit.router.require_license", new_callable=AsyncMock):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


@pytest.fixture
def lk():
    """Stub every LiveKit service call the routes make."""

    def room(gid: str) -> dict:
        return {"room_name": f"group-call-{gid}", "group_id": gid}

    stubs = {
        "create_room": AsyncMock(
            side_effect=lambda gid, *a, **k: {
                **room(gid),
                "url": "wss://t",
                "bot_token": "call-bot-secret",
                "created_at": "now",
                "is_new": False,
            }
        ),
        "get_room_info": AsyncMock(return_value=None),
        "end_room": AsyncMock(side_effect=lambda gid, *a, **k: {**room(gid), "ended_at": "now"}),
        "start_room_recording": AsyncMock(
            side_effect=lambda gid: {**room(gid), "egress_id": "e1", "output_path": "p"}
        ),
        "stop_room_recording": AsyncMock(side_effect=lambda gid: {**room(gid), "egress_id": "e1"}),
        "get_recording_info": AsyncMock(return_value=None),
    }
    with patch.multiple(livekit_service, **stubs):
        yield SimpleNamespace(**stubs)


async def _group(workspace: str, members: list[str]) -> str:
    doc = _GroupDoc(workspace=workspace, name="Room", owner=members[0])
    doc.members = list(members)
    await doc.insert()
    return str(doc.id)


def _calls(gid: str) -> list[tuple[str, str, dict | None]]:
    return [
        ("post", "/livekit/rooms", {"group_id": gid}),
        ("get", f"/livekit/rooms/{gid}", None),
        ("delete", f"/livekit/rooms/{gid}", None),
        ("post", f"/livekit/rooms/{gid}/recording/start", None),
        ("post", f"/livekit/rooms/{gid}/recording/stop", None),
        ("get", f"/livekit/rooms/{gid}/recording", None),
    ]


async def test_room_routes_refuse_a_group_in_another_workspace(client, lk) -> None:
    """S4: a member of a group in another workspace is refused on every route."""
    gid = await _group("ws-other", ["u1"])

    for method, url, body in _calls(gid):
        kwargs = {"json": body} if body is not None else {}
        resp = await client.request(method.upper(), url, **kwargs)
        assert resp.status_code == 403, (method, url, resp.status_code)
        assert resp.json()["error"]["code"] == "livekit.room_forbidden", (method, url)

    for stub in vars(lk).values():
        stub.assert_not_awaited()


async def test_room_routes_still_work_for_members_in_the_workspace(
    client, lk, workspace_id
) -> None:
    gid = await _group(workspace_id, ["u1", "u2"])

    for method, url, body in _calls(gid):
        kwargs = {"json": body} if body is not None else {}
        resp = await client.request(method.upper(), url, **kwargs)
        assert resp.status_code == 200, (method, url, resp.status_code, resp.text)
