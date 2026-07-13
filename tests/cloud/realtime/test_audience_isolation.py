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


# ---------------------------------------------------------------------------
# First-message auth frame (REVIEW-4): authenticate via the first WS frame
# instead of a URL ?token=, so the credential never lands in access logs /
# browser history / proxies. Path 3 runs ONLY when there is no ?token= and no
# paw_auth cookie; it accepts() FIRST (a frame can only be read post-handshake)
# then reads one frame under a 5s timeout.
# ---------------------------------------------------------------------------


def _passing_license(monkeypatch, router_mod):
    """Make the license gate pass so tests reach the auth logic."""

    class _Lic:
        expired = False

    monkeypatch.setattr(router_mod, "get_license", lambda: _Lic())


def _stub_post_auth(monkeypatch, router_mod):
    """No-op the connect/presence/snapshot machinery so a successfully
    authenticated socket can run through to the receive loop without a real
    ConnectionManager, workspace service, or event bus.

    ``manager`` is a MagicMock so the SYNC methods (``is_online``) return plain
    values, with the async methods explicitly wired as AsyncMocks. The
    disconnect-time presence scheduler is stubbed out — it's exercised by other
    tests and would otherwise need a real event loop task."""
    from unittest.mock import MagicMock

    mgr = MagicMock()
    mgr.is_online.return_value = False  # treat as first socket
    mgr.connect = AsyncMock()
    mgr.disconnect = AsyncMock(return_value=None)
    monkeypatch.setattr(router_mod, "manager", mgr)

    ws_service = MagicMock()
    ws_service.list_peer_ids = AsyncMock(return_value=[])
    monkeypatch.setattr(router_mod, "workspace_service", ws_service)

    monkeypatch.setattr(router_mod, "emit", AsyncMock())
    monkeypatch.setattr(router_mod, "_schedule_presence_offline", AsyncMock())
    return mgr


def _import_router():
    import importlib

    return importlib.import_module("pocketpaw_ee.cloud.chat.router")


@pytest.mark.asyncio
async def test_first_frame_ticket_authenticates_and_proceeds(monkeypatch):
    """No URL token, no cookie. A first frame {"type":"auth","ticket":...} with
    a valid ticket authenticates and the socket proceeds into the receive loop.

    The handler must accept() BEFORE reading the frame, consume the ticket via
    consume_ws_ticket, and register the connection."""
    from fastapi import WebSocketDisconnect

    router_mod = _import_router()
    _passing_license(monkeypatch, router_mod)
    mgr = _stub_post_auth(monkeypatch, router_mod)

    # Valid ticket → resolves to a user id.
    consume = AsyncMock(return_value="user-42")
    monkeypatch.setattr(router_mod, "consume_ws_ticket", consume, raising=False)
    # Also patch the lazily-imported symbol path used inside the handler.
    monkeypatch.setattr("pocketpaw_ee.cloud.auth.ws_tickets.consume_ws_ticket", consume)

    ws = AsyncMock()
    ws.cookies = {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    # First receive_text → the auth frame; second → disconnect to end the loop.
    ws.receive_text = AsyncMock(
        side_effect=[
            '{"type": "auth", "ticket": "good-ticket"}',
            WebSocketDisconnect(),
        ]
    )

    await router_mod.websocket_endpoint(ws, token=None)

    # Accepted exactly once, BEFORE auth (Path 3).
    ws.accept.assert_awaited_once()
    # Ticket was consumed with the value from the frame.
    consume.assert_awaited_once_with("good-ticket")
    # Connection registered for the resolved user → reached post-auth.
    mgr.connect.assert_awaited_once()
    assert mgr.connect.await_args.args[1] == "user-42"
    # The auth frame was consumed by the handshake, never closed with an error.
    ws.close.assert_not_called()


@pytest.mark.asyncio
async def test_first_frame_jwt_authenticates_and_proceeds(monkeypatch):
    """The first-frame form also accepts {"type":"auth","token":<jwt>}."""
    from fastapi import WebSocketDisconnect

    router_mod = _import_router()
    _passing_license(monkeypatch, router_mod)
    mgr = _stub_post_auth(monkeypatch, router_mod)

    # consume_ws_ticket should NOT be the path here; force it to None so a
    # success can only come from JWT decode.
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.auth.ws_tickets.consume_ws_ticket",
        AsyncMock(return_value=None),
    )

    import jwt as pyjwt
    from pocketpaw_ee.cloud.auth.core import SECRET

    jwt_token = pyjwt.encode(
        {"sub": "user-jwt", "aud": ["fastapi-users:auth"]},
        SECRET,
        algorithm="HS256",
    )

    ws = AsyncMock()
    ws.cookies = {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(
        side_effect=[
            f'{{"type": "auth", "token": "{jwt_token}"}}',
            WebSocketDisconnect(),
        ]
    )

    await router_mod.websocket_endpoint(ws, token=None)

    ws.accept.assert_awaited_once()
    mgr.connect.assert_awaited_once()
    assert mgr.connect.await_args.args[1] == "user-jwt"
    ws.close.assert_not_called()


@pytest.mark.asyncio
async def test_first_frame_non_auth_closes_4001(monkeypatch):
    """A first frame that is valid JSON but not an auth frame → close 4001 and
    never reach the message loop. accept() WAS called (Path 3 accepts first)."""
    router_mod = _import_router()
    _passing_license(monkeypatch, router_mod)
    mgr = _stub_post_auth(monkeypatch, router_mod)

    ws = AsyncMock()
    ws.cookies = {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(return_value='{"type": "message.send", "text": "hi"}')

    await router_mod.websocket_endpoint(ws, token=None)

    ws.accept.assert_awaited_once()
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 4001
    # Never registered the connection — unauthenticated socket stays out.
    mgr.connect.assert_not_called()


@pytest.mark.asyncio
async def test_first_frame_malformed_json_closes_4001(monkeypatch):
    """A non-JSON first frame → close 4001."""
    router_mod = _import_router()
    _passing_license(monkeypatch, router_mod)
    mgr = _stub_post_auth(monkeypatch, router_mod)

    ws = AsyncMock()
    ws.cookies = {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(return_value="not json at all")

    await router_mod.websocket_endpoint(ws, token=None)

    ws.accept.assert_awaited_once()
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 4001
    mgr.connect.assert_not_called()


@pytest.mark.asyncio
async def test_first_frame_timeout_closes_4001(monkeypatch):
    """If the client accepts but never sends an auth frame, the 5s timeout
    fires and the half-open socket is closed 4001 rather than hung."""
    router_mod = _import_router()
    _passing_license(monkeypatch, router_mod)
    mgr = _stub_post_auth(monkeypatch, router_mod)

    ws = AsyncMock()
    ws.cookies = {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()

    async def _never_returns():
        import asyncio as _a

        await _a.sleep(3600)  # outlives the 5s wait_for

    ws.receive_text = AsyncMock(side_effect=_never_returns)

    # Patch wait_for so the test doesn't actually wait 5 real seconds.
    real_wait_for = router_mod.asyncio.wait_for

    async def _fast_wait_for(coro, timeout):  # noqa: ANN001
        # Close the coroutine to avoid "never awaited" warnings, then raise
        # the same error the real wait_for raises on timeout.
        coro.close()
        raise TimeoutError

    monkeypatch.setattr(router_mod.asyncio, "wait_for", _fast_wait_for)
    try:
        await router_mod.websocket_endpoint(ws, token=None)
    finally:
        monkeypatch.setattr(router_mod.asyncio, "wait_for", real_wait_for)

    ws.accept.assert_awaited_once()
    ws.close.assert_awaited_once()
    assert ws.close.await_args.kwargs.get("code") == 4001
    mgr.connect.assert_not_called()


@pytest.mark.asyncio
async def test_url_ticket_path_still_works(monkeypatch):
    """Backward-compat: a single-use ticket in ?token= still authenticates,
    pre-accept, and proceeds — no first-frame auth involved."""
    from fastapi import WebSocketDisconnect

    router_mod = _import_router()
    _passing_license(monkeypatch, router_mod)
    mgr = _stub_post_auth(monkeypatch, router_mod)

    consume = AsyncMock(return_value="url-ticket-user")
    monkeypatch.setattr("pocketpaw_ee.cloud.auth.ws_tickets.consume_ws_ticket", consume)

    ws = AsyncMock()
    ws.cookies = {}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    # Only the loop reads here; first read disconnects.
    ws.receive_text = AsyncMock(side_effect=[WebSocketDisconnect()])

    await router_mod.websocket_endpoint(ws, token="url-ticket")

    consume.assert_awaited_once_with("url-ticket")
    ws.accept.assert_awaited_once()  # pre-accept after auth, exactly once
    mgr.connect.assert_awaited_once()
    assert mgr.connect.await_args.args[1] == "url-ticket-user"
    ws.close.assert_not_called()


@pytest.mark.asyncio
async def test_cookie_path_still_works(monkeypatch):
    """Backward-compat: the HttpOnly paw_auth cookie still authenticates,
    pre-accept, and proceeds — no first-frame auth involved."""
    from fastapi import WebSocketDisconnect

    router_mod = _import_router()
    _passing_license(monkeypatch, router_mod)
    mgr = _stub_post_auth(monkeypatch, router_mod)

    # Ticket path must not interfere (no URL token), so consume isn't relevant.
    import jwt as pyjwt
    from pocketpaw_ee.cloud.auth.core import SECRET

    cookie_jwt = pyjwt.encode(
        {"sub": "cookie-user", "aud": ["fastapi-users:auth"]},
        SECRET,
        algorithm="HS256",
    )

    ws = AsyncMock()
    ws.cookies = {"paw_auth": cookie_jwt}
    ws.accept = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_text = AsyncMock(side_effect=[WebSocketDisconnect()])

    await router_mod.websocket_endpoint(ws, token=None)

    ws.accept.assert_awaited_once()
    mgr.connect.assert_awaited_once()
    assert mgr.connect.await_args.args[1] == "cookie-user"
    ws.close.assert_not_called()
