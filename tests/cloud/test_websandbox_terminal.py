# test_websandbox_terminal.py — unit tests for the Web Cursor terminal
# WebSocket + PTY bridge slice (WC-3). Created 2026-07-15
# (feat/websandbox-terminal-ws).
#
# The auth/authz core runs on REAL Beanie over mongomock-motor (the ``mongo_db``
# fixture) with a REAL seeded user, so the connect flow exercises the genuine
# tenant/owner-scoped query paths (get_active_workspace -> get_sandbox ->
# authorize_sandbox). All Daytona interaction goes through a FAKE client + FAKE
# pty handle injected via monkeypatch — no test touches real Daytona.
#
# The endpoint is driven directly with a ``FakeWebSocket`` rather than Starlette's
# TestClient: the handler authenticates on real async Beanie and opens a pty
# before its receive loop, which the sync TestClient portal can't drive cleanly.
# Direct invocation mirrors the WC-2 provision tests (call the unit with fakes)
# and covers the whole accept -> auth -> PTY -> stream -> resize -> teardown flow.
#
# Covers:
#   * valid ticket + owned ready sandbox -> PTY opened; an input frame reaches
#     handle.send_input; the echoed VM output is forwarded as a binary frame;
#     a resize frame calls resize_pty; disconnect kills the pty (no leak).
#   * missing / invalid ticket -> closed 1008, no PTY, no accept.
#   * valid ticket but a sandbox the caller does NOT own -> denied, no PTY.
#   * not-ready row (no bound sandbox_id) -> closed 1008, no PTY.
#   * activity heartbeat is throttled (many frames -> one touch) and bumps
#     updated_at at the service level.
#   * the _ActivityThrottle unit gates to first-call-then-interval.
#
# 2026-07-15 (WC-4b): Path 3 first-message auth frame (no ?token=). Covers:
#   * no ?token= + a valid {"type":"auth","ticket":<valid>} frame -> accept()
#     first, then PTY opens (and the "token" alias works too).
#   * no ?token= + malformed / non-auth / missing first frame -> closed 4001
#     after accept, no PTY.
#   * no ?token= + auth frame with an INVALID ticket -> closed 4001, no PTY.
#   * no ?token= + valid ticket but a sandbox the caller does NOT own -> closed
#     1008 after accept, no PTY.
from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")
os.environ.setdefault("POCKETPAW_REDIS_URL", "redis://test:6379/0")

import pytest
from fastapi import WebSocketDisconnect
from pocketpaw_ee.cloud.websandbox import service as sandbox_service
from pocketpaw_ee.cloud.websandbox import ws as terminal_ws
from pocketpaw_ee.cloud.websandbox.dto import CreateSandboxRequest
from pocketpaw_ee.cloud.websandbox.ws import _ActivityThrottle, terminal_websocket_endpoint

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


class _FakeLicense:
    """A present, non-expired license (the WS gate only reads ``.expired``)."""

    expired = False


class _FakeHandle:
    """Stand-in for the SDK's AsyncPtyHandle.

    ``send_input`` records the keystrokes AND echoes them back through the
    ``on_data`` callback — modelling a real shell echo so a single input frame
    exercises both the input path (into the pty) and the output path (out to the
    socket).
    """

    def __init__(self, on_data) -> None:  # noqa: ANN001
        self._on_data = on_data
        self.sent: list[str | bytes] = []
        self.connected = True

    async def wait_for_connection(self) -> None:
        return None

    async def send_input(self, data: str | bytes) -> None:
        self.sent.append(data)
        # Echo like a real pty so the output-forwarding path is exercised.
        payload = data.encode() if isinstance(data, str) else data
        await self._on_data(b"echo:" + payload)

    async def disconnect(self) -> None:
        self.connected = False


class _FakeProcess:
    def __init__(self) -> None:
        self.handle: _FakeHandle | None = None

    async def create_pty_session(self, session_id, on_data, cwd=None, envs=None, pty_size=None):  # noqa: ANN001
        self.handle = _FakeHandle(on_data)
        return self.handle


class _FakeSandbox:
    def __init__(self) -> None:
        self.process = _FakeProcess()


@dataclass
class _FakeDaytonaClient:
    """Drop-in for DaytonaClient: holds one sandbox, records resize/kill."""

    sandbox: _FakeSandbox = field(default_factory=_FakeSandbox)
    resize_calls: list[dict] = field(default_factory=list)
    kill_calls: list[str] = field(default_factory=list)

    async def get_sandbox_instance(self, sandbox_id):  # noqa: ANN001
        return self.sandbox

    async def resize_pty(self, sandbox_id, session_id, cols, rows):  # noqa: ANN001
        self.resize_calls.append({"cols": cols, "rows": rows})

    async def kill_pty(self, sandbox_id, session_id):  # noqa: ANN001
        self.kill_calls.append(session_id)


class FakeWebSocket:
    """Minimal async WebSocket double for direct endpoint invocation.

    Feeds a queue of already-serialized client frames via ``receive_text`` and
    records ``accept`` / ``close`` / ``send_bytes`` / ``send_json``. When the
    inbound queue drains it raises ``WebSocketDisconnect`` to end the receive
    loop — the same signal Starlette raises when the browser tab closes.
    """

    def __init__(self, inbound: list[str] | None = None) -> None:
        self._inbound: deque[str] = deque(inbound or [])
        self.accepted = False
        self.closed: tuple[int, str | None] | None = None
        self.sent_bytes: list[bytes] = []
        self.sent_json: list[dict] = []
        self.cookies: dict[str, str] = {}

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = (code, reason)

    async def send_bytes(self, data: bytes) -> None:
        self.sent_bytes.append(data)

    async def send_json(self, data: dict) -> None:
        self.sent_json.append(data)

    async def receive_text(self) -> str:
        if not self._inbound:
            raise WebSocketDisconnect(1000)
        return self._inbound.popleft()


# ---------------------------------------------------------------------------
# Fixtures / seeding.
# ---------------------------------------------------------------------------


async def _seed_user(active_workspace: str | None) -> str:
    """Seed a real User doc (fastapi-users) and set its active workspace."""
    from pocketpaw_ee.cloud.auth.core import UserCreate, UserManager, get_user_db
    from pocketpaw_ee.cloud.models.user import User as UserDoc

    email = f"wc3-{os.urandom(4).hex()}@example.com"
    async for db in get_user_db():
        manager = UserManager(db)
        user = await manager.create(UserCreate(email=email, password="StrongPass123!"))
        doc = await UserDoc.get(user.id)
        doc.active_workspace = active_workspace
        await doc.save()
        return str(user.id)
    raise RuntimeError("user db iterator exhausted")  # pragma: no cover


async def _seed_ready_sandbox(workspace_id: str, user_id: str) -> str:
    """Register a ``ready`` sandbox row with a bound Daytona id; return row id."""
    view = await sandbox_service.create_sandbox(
        workspace_id,
        user_id,
        CreateSandboxRequest(
            repo="https://github.com/acme/api.git", status="ready", sandbox_id="dtn-1"
        ),
    )
    return view.id


@pytest.fixture
def wire_terminal(monkeypatch):
    """Patch the ws module's collaborators: license, ticket consume, Daytona.

    Returns the FakeDaytonaClient so tests inspect resize/kill calls. The ticket
    consume is a table lookup keyed on the token string so tests can assert the
    valid/invalid split without Redis.
    """
    client = _FakeDaytonaClient()
    tokens: dict[str, str] = {}

    async def _fake_consume(token: str) -> str | None:
        return tokens.get(token)

    monkeypatch.setattr(terminal_ws, "get_license", lambda: _FakeLicense())
    monkeypatch.setattr(terminal_ws, "consume_ws_ticket", _fake_consume)
    monkeypatch.setattr(terminal_ws, "get_daytona_client", lambda: client)
    # Fresh manager so active_count assertions are isolated per test.
    monkeypatch.setattr(terminal_ws, "manager", terminal_ws.TerminalConnectionManager())
    return client, tokens


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


async def test_valid_ticket_opens_pty_streams_and_resizes(wire_terminal) -> None:
    client, tokens = wire_terminal
    user_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", user_id)
    tokens["good-ticket"] = user_id

    ws = FakeWebSocket(
        inbound=[
            '{"type":"input","data":"echo hi\\n"}',
            '{"type":"resize","cols":120,"rows":40}',
            '{"type":"ping"}',
        ]
    )

    await terminal_websocket_endpoint(ws, row_id, token="good-ticket")

    # Socket was accepted (all auth gates passed) and never force-closed.
    assert ws.accepted is True
    assert ws.closed is None

    # Input reached the pty handle.
    handle = client.sandbox.process.handle
    assert handle is not None
    assert handle.sent == ["echo hi\n"]

    # The pty echo was forwarded to the browser as a binary frame.
    assert b"echo:echo hi\n" in ws.sent_bytes

    # Resize routed through the client wrapper.
    assert client.resize_calls == [{"cols": 120, "rows": 40}]

    # Ping answered with a pong JSON frame.
    assert {"type": "pong"} in ws.sent_json

    # Disconnect tore the pty down (no leaked session).
    assert client.kill_calls  # kill_pty was called
    assert handle.connected is False


# ---------------------------------------------------------------------------
# Auth failures — no PTY ever opens.
# ---------------------------------------------------------------------------


async def test_no_token_and_no_auth_frame_closes_4001_no_pty(wire_terminal) -> None:
    # No ?token= now routes to Path 3 (first-message auth frame). With nothing on
    # the wire, receive_text raises before any frame arrives -> 4001 after accept,
    # never the old pre-accept 1008. (Path 3 detail covered further below.)
    client, _tokens = wire_terminal
    await _seed_user("w1")  # existence irrelevant; no credential supplied

    ws = FakeWebSocket()
    await terminal_websocket_endpoint(ws, "any-row", token=None)

    assert ws.accepted is True
    assert ws.closed is not None and ws.closed[0] == 4001
    assert client.sandbox.process.handle is None


async def test_invalid_ticket_closes_1008_no_pty(wire_terminal) -> None:
    client, tokens = wire_terminal
    tokens["good-ticket"] = "someone"

    ws = FakeWebSocket()
    await terminal_websocket_endpoint(ws, "any-row", token="WRONG")

    assert ws.accepted is False
    assert ws.closed is not None and ws.closed[0] == 1008
    assert client.sandbox.process.handle is None


async def test_ticket_valid_but_sandbox_not_owned_is_denied(wire_terminal) -> None:
    client, tokens = wire_terminal
    owner_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", owner_id)

    # A DIFFERENT user in the same workspace holds a valid ticket for THIS row.
    intruder_id = await _seed_user("w1")
    tokens["intruder-ticket"] = intruder_id

    ws = FakeWebSocket(inbound=['{"type":"input","data":"whoami\\n"}'])
    await terminal_websocket_endpoint(ws, row_id, token="intruder-ticket")

    # get_sandbox is owner-scoped -> NotFound -> 1008 before any PTY.
    assert ws.accepted is False
    assert ws.closed is not None and ws.closed[0] == 1008
    assert client.sandbox.process.handle is None


async def test_not_ready_sandbox_closes_1008_no_pty(wire_terminal) -> None:
    client, tokens = wire_terminal
    user_id = await _seed_user("w1")
    # A pending row with NO bound Daytona id.
    view = await sandbox_service.create_sandbox(
        "w1",
        user_id,
        CreateSandboxRequest(repo="https://github.com/acme/api.git", status="pending"),
    )
    tokens["good-ticket"] = user_id

    ws = FakeWebSocket()
    await terminal_websocket_endpoint(ws, view.id, token="good-ticket")

    assert ws.accepted is False
    assert ws.closed is not None and ws.closed[0] == 1008
    assert client.sandbox.process.handle is None


# ---------------------------------------------------------------------------
# Path 3 — first-message auth frame (no ?token=), leak-free browser auth.
# ---------------------------------------------------------------------------


async def test_authframe_valid_opens_pty(wire_terminal) -> None:
    client, tokens = wire_terminal
    user_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", user_id)
    tokens["good-ticket"] = user_id

    # No ?token=; the ticket rides in the FIRST frame, followed by a real input.
    ws = FakeWebSocket(
        inbound=[
            '{"type":"auth","ticket":"good-ticket"}',
            '{"type":"input","data":"echo hi\\n"}',
        ]
    )

    await terminal_websocket_endpoint(ws, row_id, token=None)

    # Path 3 accepts BEFORE reading the frame; auth then succeeded, so no close.
    assert ws.accepted is True
    assert ws.closed is None

    # The auth frame was consumed by the handshake, not the input loop: only the
    # actual keystroke reached the pty.
    handle = client.sandbox.process.handle
    assert handle is not None
    assert handle.sent == ["echo hi\n"]
    assert b"echo:echo hi\n" in ws.sent_bytes

    # Disconnect tore the pty down (no leaked session).
    assert client.kill_calls
    assert handle.connected is False


async def test_authframe_token_alias_opens_pty(wire_terminal) -> None:
    # "token" is accepted as an alias for "ticket" in the auth frame (chat parity).
    client, tokens = wire_terminal
    user_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", user_id)
    tokens["good-ticket"] = user_id

    ws = FakeWebSocket(inbound=['{"type":"auth","token":"good-ticket"}'])
    await terminal_websocket_endpoint(ws, row_id, token=None)

    assert ws.accepted is True
    assert ws.closed is None
    assert client.sandbox.process.handle is not None


async def test_authframe_malformed_closes_4001_no_pty(wire_terminal) -> None:
    client, _tokens = wire_terminal
    await _seed_user("w1")

    ws = FakeWebSocket(inbound=["this is not json"])
    await terminal_websocket_endpoint(ws, "any-row", token=None)

    # Accepted (Path 3 must accept before it can read), then closed 4001.
    assert ws.accepted is True
    assert ws.closed is not None and ws.closed[0] == 4001
    assert client.sandbox.process.handle is None


async def test_authframe_non_auth_first_frame_closes_4001_no_pty(wire_terminal) -> None:
    client, _tokens = wire_terminal
    await _seed_user("w1")

    # Well-formed JSON but not an auth frame -> rejected.
    ws = FakeWebSocket(inbound=['{"type":"input","data":"whoami"}'])
    await terminal_websocket_endpoint(ws, "any-row", token=None)

    assert ws.accepted is True
    assert ws.closed is not None and ws.closed[0] == 4001
    assert client.sandbox.process.handle is None


async def test_authframe_missing_frame_closes_4001_no_pty(wire_terminal) -> None:
    client, _tokens = wire_terminal
    await _seed_user("w1")

    # Empty inbound -> receive_text raises WebSocketDisconnect before any frame
    # arrives (stands in for the client that never sends a credential).
    ws = FakeWebSocket(inbound=[])
    await terminal_websocket_endpoint(ws, "any-row", token=None)

    assert ws.accepted is True
    assert ws.closed is not None and ws.closed[0] == 4001
    assert client.sandbox.process.handle is None


async def test_authframe_invalid_ticket_closes_4001_no_pty(wire_terminal) -> None:
    client, tokens = wire_terminal
    tokens["good-ticket"] = "someone"

    ws = FakeWebSocket(inbound=['{"type":"auth","ticket":"WRONG"}'])
    await terminal_websocket_endpoint(ws, "any-row", token=None)

    assert ws.accepted is True
    assert ws.closed is not None and ws.closed[0] == 4001
    assert client.sandbox.process.handle is None


async def test_authframe_valid_ticket_not_owned_closes_1008_after_accept(wire_terminal) -> None:
    client, tokens = wire_terminal
    owner_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", owner_id)

    # A different user in the same workspace authenticates via the auth frame but
    # asks for a row they don't own: authz denial closes 1008 on the already-
    # accepted socket, and no PTY opens.
    intruder_id = await _seed_user("w1")
    tokens["intruder-ticket"] = intruder_id

    ws = FakeWebSocket(inbound=['{"type":"auth","ticket":"intruder-ticket"}'])
    await terminal_websocket_endpoint(ws, row_id, token=None)

    assert ws.accepted is True  # Path 3 accepted before the authz check
    assert ws.closed is not None and ws.closed[0] == 1008
    assert client.sandbox.process.handle is None


# ---------------------------------------------------------------------------
# Activity heartbeat — throttled, and it bumps updated_at.
# ---------------------------------------------------------------------------


async def test_activity_touch_is_throttled_to_one_per_burst(wire_terminal, monkeypatch) -> None:
    _client, tokens = wire_terminal
    user_id = await _seed_user("w1")
    row_id = await _seed_ready_sandbox("w1", user_id)
    tokens["good-ticket"] = user_id

    calls = {"n": 0}
    real_touch = sandbox_service.touch_activity

    async def _counting_touch(ws_id, uid, rid):  # noqa: ANN001
        calls["n"] += 1
        return await real_touch(ws_id, uid, rid)

    monkeypatch.setattr(terminal_ws.websandbox_service, "touch_activity", _counting_touch)

    # Five input frames in one burst — the throttle should collapse them to one
    # heartbeat write (first call touches, the rest are inside the interval).
    ws = FakeWebSocket(inbound=['{"type":"input","data":"x"}'] * 5)
    await terminal_websocket_endpoint(ws, row_id, token="good-ticket")

    assert calls["n"] == 1


async def test_touch_activity_bumps_updated_at() -> None:
    user_id = "u1"
    view = await sandbox_service.create_sandbox(
        "w1", user_id, CreateSandboxRequest(repo="r1", status="ready", sandbox_id="dtn-9")
    )

    # Force the stored updated_at into the past so a bump is observable.
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.web_sandbox import WebSandbox as _Doc

    doc = await _Doc.get(PydanticObjectId(view.id))
    old = datetime.now(UTC) - timedelta(hours=1)
    doc.updated_at = old
    await doc.save()

    touched = await sandbox_service.touch_activity("w1", user_id, view.id)
    assert touched is True

    doc2 = await _Doc.get(PydanticObjectId(view.id))

    # mongomock round-trips datetimes as tz-naive; compare tz-agnostically.
    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None)

    assert _naive(doc2.updated_at) > _naive(old)

    # A non-owning caller can't touch it.
    assert await sandbox_service.touch_activity("w1", "other", view.id) is False


# ---------------------------------------------------------------------------
# Throttle unit.
# ---------------------------------------------------------------------------


def test_activity_throttle_gates_to_interval() -> None:
    throttle = _ActivityThrottle(interval=60.0)
    assert throttle.should_touch(1000.0) is True  # first call always touches
    assert throttle.should_touch(1030.0) is False  # inside the window
    assert throttle.should_touch(1061.0) is True  # past the window
    assert throttle.should_touch(1090.0) is False  # inside the new window


# ---------------------------------------------------------------------------
# CM-2a′ snapshot-on-disconnect helper.
# ---------------------------------------------------------------------------


async def test_snapshot_on_disconnect_captures_workspace(monkeypatch) -> None:
    """A clean disconnect snapshots the row's workspace, forwarding the client."""
    sentinel_client = object()
    calls: list[dict] = []

    async def _spy_snapshot(workspace_id, user_id, row_id, *, client=None):  # noqa: ANN001
        calls.append(
            {"workspace_id": workspace_id, "user_id": user_id, "row_id": row_id, "client": client}
        )
        return "file-1"

    monkeypatch.setattr(terminal_ws.websandbox_durability, "snapshot_workspace", _spy_snapshot)

    await terminal_ws.snapshot_on_disconnect("w1", "u1", "row-1", sentinel_client)

    assert len(calls) == 1
    assert calls[0] == {
        "workspace_id": "w1",
        "user_id": "u1",
        "row_id": "row-1",
        "client": sentinel_client,
    }


async def test_snapshot_on_disconnect_swallows_failures(monkeypatch) -> None:
    """A snapshot failure on teardown is swallowed — a close never becomes an error."""

    async def _boom(workspace_id, user_id, row_id, *, client=None):  # noqa: ANN001
        raise RuntimeError("boom: VM already reaped")

    monkeypatch.setattr(terminal_ws.websandbox_durability, "snapshot_workspace", _boom)

    # Must not raise.
    await terminal_ws.snapshot_on_disconnect("w1", "u1", "row-1", None)
