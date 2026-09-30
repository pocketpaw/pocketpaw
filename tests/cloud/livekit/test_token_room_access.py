# tests/cloud/livekit/test_token_room_access.py — POST /livekit/token room access.
#
# Created 2026-09-30 (fix/livekit-call-security, MC-0 hole 1). The route checked
# group membership only when the room name started with ``group-call-``; any
# other room name got a LiveKit token with no check at all, and a member of a
# group in ANOTHER workspace passed too. Every room name must now map to a group
# the caller belongs to in the caller's workspace, else 403.
#
# The call-bot (token minted inside create_room) and guests (token minted by
# invite accept) never hit this route, so the tighter check doesn't touch them.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc

pytestmark = pytest.mark.usefixtures("mongo_db")

WS = "ws-mine"


@pytest_asyncio.fixture
async def client(mongo_db) -> AsyncClient:  # noqa: ARG001 — fixture wires Beanie
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.deps import current_user, current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.livekit.router import router as livekit_router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(livekit_router)
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id="u1", full_name="User One")
    app.dependency_overrides[current_workspace_id] = lambda: WS
    with (
        patch("pocketpaw_ee.cloud.livekit.router.require_license", new_callable=AsyncMock),
        patch(
            "pocketpaw_ee.cloud.livekit.service.generate_participant_token",
            new_callable=AsyncMock,
            return_value="lk-token",
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            yield c


async def _group(members: list[str], workspace: str = WS) -> str:
    doc = _GroupDoc(workspace=workspace, name="Room", owner=members[0])
    doc.members = list(members)
    await doc.insert()
    return str(doc.id)


def _body(room_name: str) -> dict:
    return {"room_name": room_name, "identity": "u1"}


async def test_token_refused_for_room_names_outside_group_calls(client) -> None:
    """The finding: a non ``group-call-`` room name skipped every check."""
    resp = await client.post("/livekit/token", json=_body("any-room-i-like"))
    assert resp.status_code == 403


async def test_token_refused_for_a_group_in_another_workspace(client) -> None:
    gid = await _group(["u1"], workspace="ws-other")
    resp = await client.post("/livekit/token", json=_body(f"group-call-{gid}"))
    assert resp.status_code == 403


async def test_token_refused_for_non_member_and_unknown_group(client) -> None:
    """Same 403 for "not yours" and "doesn't exist" — no existence oracle."""
    gid = await _group(["u2"])
    resp = await client.post("/livekit/token", json=_body(f"group-call-{gid}"))
    assert resp.status_code == 403
    resp = await client.post("/livekit/token", json=_body("group-call-507f1f77bcf86cd799439011"))
    assert resp.status_code == 403


async def test_token_still_issued_to_a_member(client) -> None:
    gid = await _group(["u1", "u2"])
    resp = await client.post("/livekit/token", json=_body(f"group-call-{gid}"))
    assert resp.status_code == 200
    assert resp.json()["token"] == "lk-token"
