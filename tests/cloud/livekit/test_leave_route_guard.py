"""HTTP contract tests for POST /livekit/rooms/{group_id}/leave (R-7).

New file. ``leave_call`` shipped with NO license check and NO group-membership
check while every sibling route (``generate_token``, ``get_room_info``,
``end_call``) gated on ``require_license`` + ``_require_domain_group_member``.
Since ``CallParticipantLeft`` fans out to the group's DB-resolved members, an
unguarded handler let any authenticated user push a fabricated "X left the
call" into a group they have no relationship with — injection across tenants.

These tests mount the livekit router on a bare FastAPI app (the shared
``cloud_app_client`` fixture mounts the chat agent router and overrides
``current_user_id``, the wrong dependency for this router) and exercise the
route over httpx against an isolated mongomock-motor DB.

``require_license`` is called INLINE in the handler bodies, not wired as a
``Depends`` — so ``app.dependency_overrides`` cannot reach it and the tests
patch the router module's imported name instead.

The assertions that matter are the emit-suppression ones: a 403 alone does not
prove the guard runs BEFORE the ``try``/``except Exception: pass`` around the
emit. Mutation that must break these: move the three guard calls inside the
try block, or delete ``_require_domain_group_member`` from ``leave_call``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.realtime.events import CallParticipantLeft
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc

pytestmark = pytest.mark.usefixtures("mongo_db")


@pytest_asyncio.fixture
async def client(mongo_db) -> AsyncClient:  # noqa: ARG001 — fixture wires Beanie
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.deps import current_user
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.livekit.router import router as livekit_router

    app = FastAPI()
    add_error_handler(app)
    app.include_router(livekit_router)
    # The handler reads both ``.id`` and ``.full_name`` off the user.
    app.dependency_overrides[current_user] = lambda: SimpleNamespace(id="u1", full_name="User One")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest_asyncio.fixture
async def licensed():
    """Neutralize the inline ``require_license()`` call in the router module."""
    with patch(
        "pocketpaw_ee.cloud.livekit.router.require_license",
        new_callable=AsyncMock,
        return_value=None,
    ) as m:
        yield m


async def _make_group(members: list[str]) -> str:
    doc = _GroupDoc(workspace="w-other", name="Private", owner=members[0] if members else "u9")
    doc.members = list(members)
    await doc.insert()
    return str(doc.id)


async def test_leave_rejects_non_member(client, licensed, recording_bus) -> None:
    """A user outside the group cannot inject a participant-left event."""
    gid = await _make_group(["u2", "u3"])

    resp = await client.post(f"/livekit/rooms/{gid}/leave")

    assert resp.status_code == 403
    assert not [e for e in recording_bus.events if isinstance(e, CallParticipantLeft)]


async def test_leave_rejects_unknown_group(client, licensed, recording_bus) -> None:
    """A guessed / non-existent group id 404s instead of emitting."""
    resp = await client.post("/livekit/rooms/507f1f77bcf86cd799439011/leave")

    assert resp.status_code == 404
    assert not [e for e in recording_bus.events if isinstance(e, CallParticipantLeft)]


async def test_leave_allows_member(client, licensed, recording_bus) -> None:
    """A real member still gets the normal 200 + the fan-out event."""
    gid = await _make_group(["u1", "u2"])

    resp = await client.post(f"/livekit/rooms/{gid}/leave")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    left = [e for e in recording_bus.events if isinstance(e, CallParticipantLeft)]
    assert len(left) == 1
    assert left[0].data["group_id"] == gid
    assert left[0].data["identity"] == "u1"
    assert left[0].data["name"] == "User One"


async def test_leave_requires_license(client, recording_bus) -> None:
    """No license: same 403 the siblings return, and nothing is emitted."""
    gid = await _make_group(["u1"])

    with patch(
        "pocketpaw_ee.cloud.livekit.router.require_license",
        new_callable=AsyncMock,
        side_effect=HTTPException(status_code=403, detail="Enterprise license required."),
    ):
        resp = await client.post(f"/livekit/rooms/{gid}/leave")

    assert resp.status_code == 403
    assert not [e for e in recording_bus.events if isinstance(e, CallParticipantLeft)]
