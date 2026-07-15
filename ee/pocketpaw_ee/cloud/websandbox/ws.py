# ws.py — Web Cursor terminal WebSocket endpoint + connection manager (WC-3).
# Created 2026-07-15 (feat/websandbox-terminal-ws).
#
# One authenticated WebSocket per Web Cursor session streams a REAL bash shell
# from the owning tenant's Daytona VM to the browser. Structure mirrors the chat
# WS (accept -> authenticate -> active loop -> disconnect) and reuses the same
# single-use ws_ticket auth, because browsers can't set auth headers on a WS
# upgrade.
#
# Connect flow (fail-closed at every step; a PTY is opened ONLY after all pass):
#   accept-gate: license present -> ticket in ?token= present
#   consume_ws_ticket(token) -> user_id           (single-use, Redis fail-closed)
#   auth_service.get_active_workspace(user_id)     -> workspace_id
#   websandbox_service.get_sandbox(ws, user, row)  -> row (NotFound if not owned)
#   row.sandbox_id present (row is ``ready``)      (else 1008 not-ready)
#   authorize_sandbox(ws, user, row.sandbox_id)    -> bind socket to the VM
# Any failure closes 1008 BEFORE accept/PTY — a second user's ticket for someone
# else's row is denied by get_sandbox/authorize_sandbox before any PTY opens.
#
# Message framing: client->server JSON — {"type":"input","data":"<str>"},
# {"type":"resize","cols":N,"rows":N}, {"type":"ping"}. server->client — raw
# BINARY frames carry terminal output (cleanest for xterm); pong is JSON. On
# live traffic the row's ``updated_at`` is bumped (throttled ~60s) so the WC-2
# idle reaper doesn't reclaim an active session.
#
# 2026-07-15 (WC-4a): the SAME socket also carries file operations, multiplexed
# alongside the terminal. Any ``file.*`` frame ({"type":"file.list|read|write",
# "reqId":..,"path":..[,"content":..]}) is handed to the socket-agnostic
# ``FileRpc`` (websandbox/files.py), which jails the path to the sandbox project
# dir and returns a JSON response frame (file.list.ok / file.read.ok /
# file.write.ok / file.error). File responses are typed JSON text frames while
# terminal output is binary, so the two streams never collide. No per-frame
# re-authorization — the socket is already owner-bound to the VM — but file ops
# DO bump the same throttled activity heartbeat.
from __future__ import annotations

import json
import logging
import time
import uuid

from fastapi import Query, WebSocket, WebSocketDisconnect

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.auth import service as auth_service
from pocketpaw_ee.cloud.auth.ws_tickets import consume_ws_ticket
from pocketpaw_ee.cloud.daytona.client import get_daytona_client
from pocketpaw_ee.cloud.license import get_license
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.files import FileRpc
from pocketpaw_ee.cloud.websandbox.terminal import DEFAULT_COLS, DEFAULT_ROWS, PtyBridge

logger = logging.getLogger(__name__)

# WS close codes. 1008 == policy violation (auth / authz denial). 4003 mirrors
# the chat WS "enterprise license required". 1011 == internal error (PTY open
# failed after auth succeeded).
_CLOSE_POLICY = 1008
_CLOSE_LICENSE = 4003
_CLOSE_INTERNAL = 1011

# Minimum seconds between activity heartbeats per socket (throttle): a burst of
# keystrokes becomes one ``updated_at`` write, not one per frame.
_ACTIVITY_THROTTLE_SECONDS = 60.0


class _ActivityThrottle:
    """Rate-limit the per-socket ``updated_at`` heartbeat.

    ``should_touch(now)`` returns True the first time and then at most once per
    ``_ACTIVITY_THROTTLE_SECONDS``. Kept as a tiny testable unit so the throttle
    logic is verified without a live socket.
    """

    def __init__(self, interval: float = _ACTIVITY_THROTTLE_SECONDS) -> None:
        self._interval = interval
        self._last: float | None = None

    def should_touch(self, now: float) -> bool:
        if self._last is None or (now - self._last) >= self._interval:
            self._last = now
            return True
        return False


class TerminalConnectionManager:
    """Tracks the live pty bridge behind each terminal socket for teardown.

    Deliberately minimal versus the chat ``ConnectionManager`` — the terminal
    has no presence, no rooms, no fan-out. Its one job is to guarantee that
    every accepted socket's pty session is killed exactly once when the socket
    goes away, so a dropped browser tab never leaks a shell in the VM.
    """

    def __init__(self) -> None:
        self._bridges: dict[WebSocket, PtyBridge] = {}

    def register(self, websocket: WebSocket, bridge: PtyBridge) -> None:
        self._bridges[websocket] = bridge

    async def unregister(self, websocket: WebSocket) -> None:
        bridge = self._bridges.pop(websocket, None)
        if bridge is not None:
            await bridge.close()

    def active_count(self) -> int:
        return len(self._bridges)


# Module-level singleton (mirrors chat.ws.manager).
manager = TerminalConnectionManager()


async def terminal_websocket_endpoint(
    websocket: WebSocket,
    row_id: str,
    token: str | None = Query(None),
) -> None:
    """Stream a real bash PTY from the owning sandbox's VM to the browser.

    See the module docstring for the full connect/auth flow. The ws_ticket is
    consumed (single-use) BEFORE accept; any auth/authz failure closes 1008 and
    never opens a PTY.
    """
    # License gate — parity with the REST /websandbox routes and chat WS.
    lic = get_license()
    if lic is None or lic.expired:
        await websocket.close(code=_CLOSE_LICENSE, reason="Enterprise license required")
        return

    # Path 1 — single-use ws_ticket in ?token= (the only auth path the browser
    # can use on a cross-origin WS upgrade). No ticket -> no access.
    if not token:
        await websocket.close(code=_CLOSE_POLICY, reason="Missing ticket")
        return
    user_id = await consume_ws_ticket(token)
    if not user_id:
        await websocket.close(code=_CLOSE_POLICY, reason="Invalid ticket")
        return

    # Resolve the caller's workspace from the same membership field the HTTP
    # request context reads. Fail closed if the user has no active workspace.
    try:
        workspace_id = await auth_service.get_active_workspace(user_id)
    except CloudError:
        workspace_id = None
    if not workspace_id:
        await websocket.close(code=_CLOSE_POLICY, reason="No active workspace")
        return

    # Resolve + authorize the sandbox row. get_sandbox is tenant+owner scoped
    # (NotFound for a row the caller doesn't own); authorize_sandbox is the
    # fail-closed oracle bound to the Daytona id. Either denial -> 1008, no PTY.
    try:
        row = await websandbox_service.get_sandbox(workspace_id, user_id, row_id)
        if not row.sandbox_id:
            await websocket.close(code=_CLOSE_POLICY, reason="Sandbox not ready")
            return
        await websandbox_service.authorize_sandbox(workspace_id, user_id, row.sandbox_id)
    except CloudError:
        await websocket.close(code=_CLOSE_POLICY, reason="Access denied")
        return

    client = get_daytona_client()
    if client is None:
        await websocket.close(code=_CLOSE_LICENSE, reason="Sandbox runtime unavailable")
        return

    # Every gate passed — accept the socket and open the PTY.
    await websocket.accept()

    session_id = f"term-{uuid.uuid4().hex}"

    async def _sink(data: bytes) -> None:
        # Raw terminal bytes go to the browser as binary frames (xterm-friendly).
        await websocket.send_bytes(data)

    bridge = PtyBridge(client, row.sandbox_id, session_id, _sink)
    try:
        await bridge.start(cols=DEFAULT_COLS, rows=DEFAULT_ROWS)
    except Exception:
        logger.exception("pty open failed: row=%s sandbox=%s", row_id, row.sandbox_id)
        await bridge.close()
        await websocket.close(code=_CLOSE_INTERNAL, reason="Failed to open terminal")
        return

    manager.register(websocket, bridge)
    throttle = _ActivityThrottle()

    # File ops share this socket. FileRpc jails every path to the sandbox project
    # dir (resolved lazily on first use); it reuses the already-owner-authorized
    # client + sandbox_id and never re-authorizes per frame.
    file_rpc = FileRpc(client, row.sandbox_id)

    async def _touch() -> None:
        """Bump the row's activity heartbeat, throttled, swallowing errors."""
        if not throttle.should_touch(time.monotonic()):
            return
        try:
            await websandbox_service.touch_activity(workspace_id, user_id, row_id)
        except Exception:  # noqa: BLE001 — a heartbeat miss must not drop the session
            logger.debug("activity touch failed for row=%s", row_id)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue  # ignore malformed frames rather than tearing down
            if not isinstance(msg, dict):
                continue

            mtype = msg.get("type")
            if isinstance(mtype, str) and mtype.startswith("file."):
                # File op multiplexed on the terminal socket. FileRpc jails the
                # path and returns a JSON response frame (or file.error); a bad
                # frame never tears the socket down.
                response = await file_rpc.dispatch(msg)
                if response is not None:
                    await websocket.send_json(response)
                    await _touch()
                continue
            if mtype == "input":
                data = msg.get("data")
                if isinstance(data, str):
                    await bridge.send_input(data)
                    await _touch()
            elif mtype == "resize":
                cols = msg.get("cols")
                rows = msg.get("rows")
                if isinstance(cols, int) and isinstance(rows, int) and cols > 0 and rows > 0:
                    await bridge.resize(cols, rows)
                    await _touch()
            elif mtype == "ping":
                # Keep the socket warm through idle-closing edge proxies.
                await websocket.send_json({"type": "pong"})
    except WebSocketDisconnect:
        pass
    except RuntimeError as exc:
        # Starlette raises RuntimeError("WebSocket is not connected") when
        # receive is called after the peer already disconnected. Treat as a
        # normal close; re-raise anything else.
        if "not connected" not in str(exc).lower():
            logger.exception("terminal WS error: row=%s", row_id)
    except Exception:
        logger.exception("terminal WS error: row=%s", row_id)
    finally:
        # Teardown: kill the pty session so the shell doesn't leak in the VM.
        await manager.unregister(websocket)


__all__ = ["TerminalConnectionManager", "manager", "terminal_websocket_endpoint"]
