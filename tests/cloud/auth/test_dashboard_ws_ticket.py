"""First-frame ws_ticket auth on the dashboard socket (/api/v1/ws).

Created: 2026-10-03 — covers the new first-frame path in
``pocketpaw.dashboard_ws.websocket_handler``: a real ticket minted by
``mint_ws_ticket`` (fakeredis-backed) works once, a replay is refused, a ticket
in ``?token=`` is refused at the handshake, a bad or missing first frame closes
4001 without hanging, and the existing token/middleware auth is unchanged.
"""

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

from unittest.mock import patch

import fakeredis
import fakeredis.aioredis
import pytest
from fastapi.testclient import TestClient
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud.auth.ws_tickets import mint_ws_ticket
from starlette.websockets import WebSocketDisconnect

_TOKEN = "dash-ws-ticket-test-token"


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    from pocketpaw.security.rate_limiter import ws_limiter

    ws_limiter._buckets.clear()


@pytest.fixture
def fake_redis(monkeypatch):
    # Fresh client per call over one shared server: minting runs on a private
    # loop and consuming on the TestClient's loop, so no client may be reused.
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        redis_client,
        "get_redis",
        lambda: fakeredis.aioredis.FakeRedis(server=server, decode_responses=True),
    )
    return server


@pytest.fixture
def client(fake_redis):  # noqa: ARG001
    from pocketpaw.dashboard import app

    with (
        patch("pocketpaw.dashboard_auth.get_access_token", return_value=_TOKEN),
        patch("pocketpaw.dashboard.get_access_token", return_value=_TOKEN),
    ):
        yield TestClient(app, raise_server_exceptions=False)


def _run(coro):
    import asyncio

    return asyncio.new_event_loop().run_until_complete(coro)


def _close_code(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as exc:
        ws.receive_json()
    return exc.value.code


def test_valid_ticket_first_frame_connects(client):
    ticket = _run(mint_ws_ticket("user-1"))
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "auth", "ticket": ticket})
        assert ws.receive_json()["type"] == "connection_info"


def test_replayed_ticket_rejected(client):
    ticket = _run(mint_ws_ticket("user-1"))
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "auth", "ticket": ticket})
        assert ws.receive_json()["type"] == "connection_info"
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "auth", "ticket": ticket})
        assert _close_code(ws) == 4001


def test_ticket_in_query_string_refused(client):
    ticket = _run(mint_ws_ticket("user-1"))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/v1/ws?token={ticket}") as ws:
            ws.receive_json()
    assert exc.value.code == 4003


@pytest.mark.parametrize(
    "frame",
    ["not json", '{"type": "chat", "ticket": "x"}', '{"type": "auth"}', '["auth"]'],
)
def test_bad_first_frame_closes_4001(client, frame):
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_text(frame)
        assert _close_code(ws) == 4001


def test_forged_ticket_closes_4001(client):
    with client.websocket_connect("/api/v1/ws") as ws:
        ws.send_json({"type": "auth", "ticket": "forged.jwt.value"})
        assert _close_code(ws) == 4001


def test_missing_first_frame_times_out(client, monkeypatch):
    monkeypatch.setattr("pocketpaw.dashboard_ws.AUTH_FRAME_TIMEOUT_SECONDS", 0.2)
    with client.websocket_connect("/api/v1/ws") as ws:
        assert _close_code(ws) == 4001


def test_existing_query_token_still_works(client):
    with client.websocket_connect(f"/api/v1/ws?token={_TOKEN}") as ws:
        assert ws.receive_json()["type"] == "connection_info"


def test_existing_cookie_still_works(client):
    client.cookies.set("pocketpaw_session", _TOKEN)
    with client.websocket_connect("/api/v1/ws") as ws:
        assert ws.receive_json()["type"] == "connection_info"


def test_bad_query_token_still_4003(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/v1/ws?token=wrong") as ws:
            ws.receive_json()
    assert exc.value.code == 4003


def test_legacy_ws_path_still_gated_by_middleware(client):
    ticket = _run(mint_ws_ticket("user-1"))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws") as ws:
            ws.send_json({"type": "auth", "ticket": ticket})
            ws.receive_json()
    assert exc.value.code == 4003
