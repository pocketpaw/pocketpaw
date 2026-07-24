# ws.py — Web Cursor terminal WebSocket endpoint + connection manager (WC-3).
# Created 2026-07-15 (feat/websandbox-terminal-ws).
#
# One authenticated WebSocket per Web Cursor session streams a REAL bash shell
# from the owning tenant's Daytona VM to the browser. Structure mirrors the chat
# WS (accept -> authenticate -> active loop -> disconnect) and reuses the same
# single-use ws_ticket auth, because browsers can't set auth headers on a WS
# upgrade.
#
# 2026-07-15 (WC-4b): the ticket now resolves via EITHER of two auth paths, so
# the browser can authenticate without leaking the ticket in the URL (URL query
# strings leak into access logs, browser history, and proxies — the same
# REVIEW-4 reasoning that moved the chat WS off URL tickets):
#   * Path 1 — single-use ws_ticket in ``?token=``, consumed BEFORE accept
#     (unchanged; the only path a cross-origin WS upgrade can carry in the URL).
#   * Path 3 — no ``?token=``: accept() FIRST (a frame can only be read after the
#     handshake completes), then read the first frame under a 5s timeout and
#     parse ``{"type":"auth","ticket"|"token":"..."}``. On timeout, a malformed
#     frame, a non-auth frame, or a failed ``consume_ws_ticket`` -> close 4001 and
#     the socket never reaches the message loop.
# ``websocket.accept()`` is called exactly once on every path, tracked by the
# ``accepted`` flag: Path 1 accepts AFTER the authz checks (as before); Path 3
# accepts BEFORE reading the frame, so the shared authz section must not accept
# again.
#
# Connect flow (fail-closed at every step; a PTY is opened ONLY after all pass):
#   license present                                (else 4003)
#   Path 1: ticket in ?token= -> consume_ws_ticket (pre-accept; 1008 on failure)
#   Path 3: no ?token= -> accept, read first auth   (post-accept; 4001 on failure)
#           frame under 5s timeout -> consume_ws_ticket
#   -> user_id                                     (single-use, Redis fail-closed)
#   auth_service.get_active_workspace(user_id)     -> workspace_id
#   websandbox_service.get_sandbox(ws, user, row)  -> row (NotFound if not owned)
#   row.sandbox_id present (row is ``ready``)      (else 1008 not-ready)
#   authorize_sandbox(ws, user, row.sandbox_id)    -> bind socket to the VM
# A second user's ticket for someone else's row is denied by get_sandbox/
# authorize_sandbox before any PTY opens. Path 1 closes 1008 pre-accept; Path 3
# is already accepted, so an authz denial closes 1008 on the accepted socket.
#
# Message framing: client->server JSON — {"type":"input","data":"<str>"},
# {"type":"resize","cols":N,"rows":N}, {"type":"ping"}. server->client — raw
# BINARY frames carry terminal output (cleanest for xterm); pong is JSON. On
# live traffic the row's ``updated_at`` is bumped (throttled ~60s) so the WC-2
# idle reaper doesn't reclaim an active session.
#
# 2026-07-15 (WC-4a): the SAME socket also carries file operations, multiplexed
# alongside the terminal. Any ``file.*`` frame ({"type":"file.list|read|write|
# create|delete|move","reqId":..,"path":..[,"content"|"isDir"|"toPath":..]}) is
# handed to the socket-agnostic ``FileRpc`` (websandbox/files.py), which jails the
# path to the sandbox project dir and returns a JSON response frame (file.<op>.ok
# / file.error). File responses are typed JSON text frames while terminal output
# is binary, so the two streams never collide. No per-frame re-authorization — the
# socket is already owner-bound to the VM — but file ops DO bump the same throttled
# activity heartbeat.
#
# 2026-07-16 (WC-4c): create/delete/move join the file verbs. ws.py binds three
# best-effort durability closures onto FileRpc — ``_mirror`` (on_write),
# ``_drop`` (on_delete → overlay drop), ``_move`` (on_move → overlay re-key) — so
# a create/save is mirrored, a delete isn't resurrected, and a rename replays at
# its new path on restore.
#
# 2026-07-25 (B2, feat/code-daytona-project-anchor): those durability hooks — and
# the disconnect snapshot — now target the DURABLE PROJECT instead of the ephemeral
# sandbox row. The row is unique per (workspace, user, repo), and a scaffold project
# puts a TEMPLATE id in ``repo``, so N projects from one starter shared a single row
# and would have overwritten each other's snapshot + overlay. The socket only knows
# its row, so it resolves the owning project once at connect time
# (``codeproject_service.find_project_for_sandbox``) and routes every durability
# write through the project id from then on.
#
# The socket DEGRADES rather than dropping writes: a sandbox opened outside the
# project flow (the plain ``/websandbox`` REST surface) has no owning project, so
# the hooks fall back to the sandbox-keyed functions exactly as before. Losing a
# user's edits is a far worse failure than writing them to the older anchor. The
# anchor choice is made once, in ``build_durability_hooks``, so it is a single
# testable decision rather than three copies of the same branch.
#
# 2026-07-25 (B4, feat/code-cross-runtime-restore): a FAILED durable write is no
# longer invisible. The hooks are wrapped in ``_visible_durability`` and the
# disconnect snapshot logs at WARNING instead of debug. WHY: the project store fails
# CLOSED on a cloud whose ``POCKETPAW_UPLOAD_ADAPTER`` isn't ``s3``, and both the
# per-file hooks (via ``FileRpc``) and the disconnect snapshot swallow that — so a
# misconfigured deploy persisted NOTHING while every save reported ok, with the only
# trace at debug. Still swallowed (a durability hiccup must not break a file write
# that already landed); just diagnosable now, with the anchor and path in the line.
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Query, WebSocket, WebSocketDisconnect

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.auth import service as auth_service
from pocketpaw_ee.cloud.auth.ws_tickets import consume_ws_ticket
from pocketpaw_ee.cloud.codeproject import service as codeproject_service
from pocketpaw_ee.cloud.daytona.client import DaytonaClient, get_daytona_client
from pocketpaw_ee.cloud.license import get_license
from pocketpaw_ee.cloud.websandbox import durability as websandbox_durability
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.files import FileRpc
from pocketpaw_ee.cloud.websandbox.terminal import DEFAULT_COLS, DEFAULT_ROWS, PtyBridge

logger = logging.getLogger(__name__)

# WS close codes. 1008 == policy violation (authz denial). 4001 == the Path 3
# first-frame auth failed (mirrors the chat WS auth-frame close). 4003 mirrors
# the chat WS "enterprise license required". 1011 == internal error (PTY open
# failed after auth succeeded).
_CLOSE_POLICY = 1008
_CLOSE_AUTH_FRAME = 4001
_CLOSE_LICENSE = 4003
_CLOSE_INTERNAL = 1011

# Seconds to wait for the Path 3 first-message auth frame before giving up. A
# half-open socket must not hold resources waiting for a credential that may
# never arrive. Mirrors the chat WS 5s auth-frame timeout.
_AUTH_FRAME_TIMEOUT_SECONDS = 5.0

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


async def resolve_owning_project(workspace_id: str, user_id: str, row_id: str) -> str | None:
    """The durable project this sandbox row belongs to, or ``None`` (B2).

    The socket is opened against a WebSandbox ROW, but durable state is anchored on
    the PROJECT, so the anchor has to be resolved once at connect time. ``None``
    means "no owning project" — a sandbox opened straight off the ``/websandbox``
    REST surface — and the caller degrades to the sandbox-keyed path.

    A lookup FAILURE also resolves to ``None`` on purpose: degrading to the older
    anchor still persists the user's edits somewhere, whereas propagating the error
    would refuse the socket over a durability detail.
    """
    try:
        project = await codeproject_service.find_project_for_sandbox(workspace_id, user_id, row_id)
    except Exception:  # noqa: BLE001 — a resolution miss degrades, never denies
        logger.warning(
            "websandbox.ws: owning-project lookup failed for row=%s; "
            "falling back to sandbox-keyed durability",
            row_id,
            exc_info=True,
        )
        return None
    return project.id if project is not None else None


def _visible_durability(op: str, anchor: str, hook: Any) -> Any:
    """Wrap a durability hook so a failure is swallowed but never INVISIBLE (B4).

    ``FileRpc`` already swallows a hook failure at DEBUG, which is right about the
    swallowing (a durability hiccup must not turn a landed file write into a client
    error) and wrong about the level. The project store fails CLOSED on a
    misconfigured cloud (``POCKETPAW_UPLOAD_ADAPTER != s3`` → a 503 from
    ``_require_s3_for_project_store``), so EVERY save on such a deploy raises here:
    the user's edits are not persisted anywhere and the only trace is a debug line
    nobody has enabled. Logging at WARNING with the anchor (durable project, or the
    sandbox row when degraded) and the path makes that diagnosable from ordinary
    production logs. Swallowing is unchanged.
    """

    async def _wrapped(*args: Any) -> None:
        try:
            await hook(*args)
        except Exception:  # noqa: BLE001 — durability is best-effort; the file op landed
            logger.warning(
                "websandbox.durability: %s failed for %s path=%r — the edit is NOT persisted",
                op,
                anchor,
                args[0] if args else "?",
                exc_info=True,
            )

    return _wrapped


def build_durability_hooks(
    workspace_id: str,
    user_id: str,
    row_id: str,
    project_id: str | None,
    uploads: Any,
) -> tuple[
    Callable[[str, bytes], Awaitable[None]],
    Callable[[str], Awaitable[None]],
    Callable[[str, str], Awaitable[None]],
]:
    """Build the (on_write, on_delete, on_move) FileRpc durability hooks (B2).

    ONE place decides the anchor. With a ``project_id`` every editor save, delete,
    and rename is written through against the durable project — the fix for N
    projects on one starter template sharing a single repo-keyed sandbox row. With
    ``None`` (a sandbox opened outside the project flow) the hooks keep the
    original sandbox-keyed behaviour: degrade, never drop the write.

    All three are best-effort — a durability failure must never fail the file op
    itself — but they are wrapped in ``_visible_durability`` so the failure is
    LOGGED AT WARNING with its anchor and path instead of vanishing into debug.
    """
    anchor = f"project={project_id}" if project_id else f"sandbox_row={row_id}"

    if project_id:

        async def _mirror(rel_path: str, data: bytes) -> None:
            await websandbox_durability.mirror_file_to_project(
                workspace_id, user_id, project_id, rel_path, data, uploads=uploads
            )

        async def _drop(rel_path: str) -> None:
            await websandbox_durability.drop_project_overlay(
                workspace_id, user_id, project_id, rel_path
            )

        async def _move(src_rel: str, dst_rel: str) -> None:
            await websandbox_durability.move_project_overlay(
                workspace_id, user_id, project_id, src_rel, dst_rel
            )

        return (
            _visible_durability("write-through mirror", anchor, _mirror),
            _visible_durability("overlay drop", anchor, _drop),
            _visible_durability("overlay re-key", anchor, _move),
        )

    async def _mirror_row(rel_path: str, data: bytes) -> None:
        await websandbox_durability.mirror_file(
            workspace_id, user_id, row_id, rel_path, data, uploads=uploads
        )

    async def _drop_row(rel_path: str) -> None:
        # Delete-side durability: drop the overlay entry so a deleted file is not
        # resurrected on restore (WC-4c).
        await websandbox_durability.drop_overlay(workspace_id, user_id, row_id, rel_path)

    async def _move_row(src_rel: str, dst_rel: str) -> None:
        # Rename-side durability: re-key the overlay entry to the new path so
        # restore replays the file where it now lives (WC-4c).
        await websandbox_durability.move_overlay(workspace_id, user_id, row_id, src_rel, dst_rel)

    return (
        _visible_durability("write-through mirror", anchor, _mirror_row),
        _visible_durability("overlay drop", anchor, _drop_row),
        _visible_durability("overlay re-key", anchor, _move_row),
    )


async def snapshot_on_disconnect(
    workspace_id: str,
    user_id: str,
    row_id: str,
    client: DaytonaClient | None,
    *,
    project_id: str | None = None,
    daytona_id: str | None = None,
) -> None:
    """Best-effort workspace snapshot when a terminal socket closes (CM-2a′, B2).

    The durable half of Code Mode is the S3 snapshot; the Daytona VM is pure
    scratch. With the aggressive Daytona lifecycle (stop 5 / delete-on-stop), a
    disconnected VM is reclaimed within minutes — so a CLEAN disconnect (tab
    close, navigate away) is the moment to capture the workspace while the VM is
    still alive. ``codeproject.lifecycle.open_project`` restores the latest
    snapshot on the next open.

    The pointer lands on the durable PROJECT when the socket resolved one, keyed by
    ``project_id`` and taken from the live VM ``daytona_id`` (the project-keyed
    snapshot addresses the VM directly rather than through the row). Without both —
    a sandbox opened outside the project flow, or a row with no bound VM — it falls
    back to the sandbox-keyed snapshot rather than skipping the capture.

    Best-effort by design: a snapshot failure (VM already gone, S3 down, an
    unprovisioned row) must NEVER surface on socket teardown — it is swallowed. It
    is logged at WARNING though (B4): this is the capture that makes a whole
    session's work durable, so losing it silently is indistinguishable from data
    loss on the next open.
    """
    try:
        if project_id and daytona_id:
            await websandbox_durability.snapshot_project(
                workspace_id, user_id, project_id, daytona_id, client=client
            )
        else:
            await websandbox_durability.snapshot_workspace(
                workspace_id, user_id, row_id, client=client
            )
    except Exception:  # noqa: BLE001 — teardown must never raise on a snapshot miss
        logger.warning(
            "websandbox.snapshot on disconnect failed for row=%s project=%s daytona=%s — "
            "this session's workspace was NOT captured",
            row_id,
            project_id,
            daytona_id,
            exc_info=True,
        )


async def terminal_websocket_endpoint(
    websocket: WebSocket,
    row_id: str,
    token: str | None = Query(None),
) -> None:
    """Stream a real bash PTY from the owning sandbox's VM to the browser.

    See the module docstring for the full connect/auth flow. The ws_ticket
    resolves via Path 1 (``?token=``, consumed pre-accept) or Path 3 (a
    first-message auth frame read post-accept under a 5s timeout). Any auth
    failure closes 1008 (Path 1) or 4001 (Path 3); any authz failure closes 1008
    and never opens a PTY.
    """
    # License gate first on BOTH paths — parity with the REST /websandbox routes
    # and chat WS.
    lic = get_license()
    if lic is None or lic.expired:
        await websocket.close(code=_CLOSE_LICENSE, reason="Enterprise license required")
        return

    # ``accepted`` tracks whether accept() has run so accept() happens exactly
    # once: Path 1 accepts AFTER the authz checks below; Path 3 accepts BEFORE it
    # can read the first frame, then the shared authz section must not re-accept.
    accepted = False

    if token:
        # Path 1 — single-use ws_ticket in ?token=. Consumed BEFORE accept, so a
        # bad ticket is refused during the handshake (close 1008, no accept).
        user_id = await consume_ws_ticket(token)
        if not user_id:
            await websocket.close(code=_CLOSE_POLICY, reason="Invalid ticket")
            return
    else:
        # Path 3 — no URL token. Authenticate from the first frame so the ticket
        # never travels in the URL. We MUST accept() before we can receive, and
        # the 5s timeout stops a half-open socket from holding resources while we
        # wait for a credential that may never come.
        await websocket.accept()
        accepted = True
        try:
            raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=_AUTH_FRAME_TIMEOUT_SECONDS
            )
            frame = json.loads(raw)
        except Exception:
            # Timeout, disconnect, or non-JSON first frame.
            await websocket.close(code=_CLOSE_AUTH_FRAME, reason="Missing auth")
            return

        if not isinstance(frame, dict) or frame.get("type") != "auth":
            await websocket.close(code=_CLOSE_AUTH_FRAME, reason="Missing auth")
            return

        # Accept ``token`` as an alias for ``ticket`` (parity with the chat WS
        # auth frame); either carries the single-use ws_ticket.
        ticket = frame.get("ticket") or frame.get("token")
        user_id = await consume_ws_ticket(ticket) if isinstance(ticket, str) and ticket else None
        if not user_id:
            await websocket.close(code=_CLOSE_AUTH_FRAME, reason="Invalid ticket")
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

    # B2: resolve the DURABLE anchor once, now that the row is owner-authorized.
    # Every durability write below (mirror / drop / move / disconnect snapshot)
    # targets this project id; ``None`` degrades to the sandbox-keyed path.
    project_id = await resolve_owning_project(workspace_id, user_id, row_id)

    # Every gate passed — accept the socket (unless Path 3 already did) and open
    # the PTY. ``accepted`` keeps accept() exactly-once across both paths.
    if not accepted:
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
    #
    # Write-through durability (CM-2a′, re-anchored in B2): build ONE overlay
    # uploads service for the session and pass FileRpc hooks bound to this tenant
    # and to the DURABLE PROJECT that owns this row (resolved once, above). Each
    # editor save then also lands in blob storage against the project; a mirror
    # failure is swallowed inside FileRpc so it never fails the save.
    overlay_uploads = websandbox_durability.build_uploads()
    _mirror, _drop, _move = build_durability_hooks(
        workspace_id, user_id, row_id, project_id, overlay_uploads
    )

    file_rpc = FileRpc(client, row.sandbox_id, on_write=_mirror, on_delete=_drop, on_move=_move)

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
        # CM-2a′ durability, project-anchored (B2): capture the workspace to S3 on
        # this clean disconnect so a returning user restores their uncommitted
        # work. Best-effort — a snapshot miss never turns a normal close into an
        # error.
        await snapshot_on_disconnect(
            workspace_id,
            user_id,
            row_id,
            client,
            project_id=project_id,
            daytona_id=row.sandbox_id,
        )


__all__ = [
    "TerminalConnectionManager",
    "build_durability_hooks",
    "manager",
    "resolve_owning_project",
    "snapshot_on_disconnect",
    "terminal_websocket_endpoint",
]
