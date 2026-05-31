"""Regression: realtime fan-out must not leak events to non-audience users.

The WebSocket path at ``/ws/cloud`` is authenticated at handshake time via a
JWT in the query string (see ``chat/router.py::websocket_endpoint``). Once a
client is connected, the only "subscription" surface is the audience-keyed
bus fan-out in ``_core/realtime/bus.py::InProcessBus.publish``: each event is
resolved to a list of ``user_id`` recipients and the connection manager is
told to ``send_to_user(uid, payload)`` for each one. There is no per-channel
client-side subscribe step a stranger could spoof to receive other tenants'
events.

These tests pin that behaviour. If a future refactor accidentally turns the
realtime layer into a broadcast bus (e.g. "send to every connected socket"),
or skips the resolver, or routes by socket-supplied ``group_id`` instead of
DB-resolved membership, these tests fail loudly.

The companion membership checks for *inbound* client → server messages
(``room.join``, ``typing.*``, ``read.ack``) live in
``tests/cloud/chat/test_room_scoped.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
from pocketpaw_ee.cloud._core.realtime.bus import InProcessBus
from pocketpaw_ee.cloud._core.realtime.events import (
    MessageNew,
    WorkspaceUpdated,
)


@pytest.mark.asyncio
async def test_workspace_event_not_delivered_to_stranger():
    """A workspace.updated event must reach only workspace members.

    Setup: workspace ``w-alpha`` has members [u-alice, u-bob]. An unrelated
    user u-mallory has an active WebSocket but is NOT in w-alpha.
    """
    members_by_workspace = {"w-alpha": ["u-alice", "u-bob"]}

    async def workspace_members(wid: str) -> list[str]:
        return list(members_by_workspace.get(wid, []))

    resolver = AudienceResolver(workspace_members=workspace_members)
    conn = AsyncMock()
    bus = InProcessBus(resolver=resolver, conn_manager=conn)

    await bus.publish(WorkspaceUpdated(data={"workspace_id": "w-alpha"}))

    recipients = {call.args[0] for call in conn.send_to_user.await_args_list}
    assert recipients == {"u-alice", "u-bob"}
    assert "u-mallory" not in recipients


@pytest.mark.asyncio
async def test_message_new_not_delivered_to_non_group_member():
    """message.new for group g1 must reach only g1 members.

    A stranger holding a live socket cannot receive g1's messages because the
    fan-out is keyed by ``group_members(g1)``, not by who happens to be online.
    """
    members_by_group = {"g1": ["u-alice", "u-bob"]}

    async def group_members(gid: str) -> list[str]:
        return list(members_by_group.get(gid, []))

    resolver = AudienceResolver(group_members=group_members)
    conn = AsyncMock()
    bus = InProcessBus(resolver=resolver, conn_manager=conn)

    # message.new excludes the sender from the audience; u-bob should still get it.
    await bus.publish(MessageNew(data={"group_id": "g1", "sender": "u-alice"}))

    recipients = {call.args[0] for call in conn.send_to_user.await_args_list}
    assert recipients == {"u-bob"}
    assert "u-mallory" not in recipients
    assert "u-alice" not in recipients  # sender excluded by resolver


@pytest.mark.asyncio
async def test_event_for_unknown_workspace_delivers_to_nobody():
    """If the resolver returns [] for a workspace, nobody gets the event.

    This pins the failure mode: if a service emits a workspace event with a
    bogus workspace_id, the fan-out is a no-op rather than a broadcast.
    """

    async def workspace_members(_wid: str) -> list[str]:
        return []

    resolver = AudienceResolver(workspace_members=workspace_members)
    conn = AsyncMock()
    bus = InProcessBus(resolver=resolver, conn_manager=conn)

    await bus.publish(WorkspaceUpdated(data={"workspace_id": "w-ghost"}))

    conn.send_to_user.assert_not_awaited()


# ---------------------------------------------------------------------------
# Connect-time JWT verification (handshake auth)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws_handshake_rejects_invalid_token(monkeypatch):
    """The /ws/cloud handshake must reject tokens that fail JWT verification.

    Connect-time auth is the gate that makes audience-keyed fan-out trustworthy:
    the bus delivers to ``user_id``, but ``user_id`` is only meaningful if the
    socket really belongs to that user. ``websocket_endpoint`` decodes the JWT
    with ``AUTH_SECRET`` and closes with 4001 on any failure.
    """
    import importlib

    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    # License gate must pass so we reach the JWT check.
    class _Lic:
        expired = False

    monkeypatch.setattr(router_mod, "get_license", lambda: _Lic())
    monkeypatch.setenv("AUTH_SECRET", "test-secret-for-realtime-isolation")

    ws = AsyncMock()
    ws.close = AsyncMock()
    ws.accept = AsyncMock()

    # Bogus token — JWT decode raises, handler must close before accept().
    await router_mod.websocket_endpoint(ws, token="not-a-jwt")

    ws.close.assert_awaited_once()
    close_kwargs = ws.close.await_args.kwargs
    assert close_kwargs.get("code") == 4001
    ws.accept.assert_not_called()


@pytest.mark.asyncio
async def test_ws_handshake_rejects_when_license_missing(monkeypatch):
    """No enterprise license → close 4003, never touch the JWT or connect."""
    import importlib

    router_mod = importlib.import_module("pocketpaw_ee.cloud.chat.router")

    monkeypatch.setattr(router_mod, "get_license", lambda: None)

    ws = AsyncMock()
    ws.close = AsyncMock()
    ws.accept = AsyncMock()

    await router_mod.websocket_endpoint(ws, token="anything")

    ws.close.assert_awaited_once()
    close_kwargs = ws.close.await_args.kwargs
    assert close_kwargs.get("code") == 4003
    ws.accept.assert_not_called()
