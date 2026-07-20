# terminal.py — Web Cursor PTY-over-WebSocket bridge (WC-3).
# Created 2026-07-15 (feat/websandbox-terminal-ws).
#
# This is the socket-agnostic core of the terminal slice: it opens a REAL bash
# PTY session inside a Daytona VM and pumps its output to an injected async
# ``sink``. ``ws.py`` wires the sink to ``websocket.send_bytes`` for the browser;
# the live-smoke script wires it to an in-memory buffer — the SAME code path,
# minus the socket.
#
# Why the bridge holds the handle directly (the "handle gap"):
# ``DaytonaClient.create_pty_session`` DISCARDS the ``AsyncPtyHandle`` the SDK
# returns (it returns None), so you cannot send keystrokes through the shared
# client wrapper. Rather than change that shared method (WC-2 and others depend
# on its signature), this bridge grabs the sandbox instance itself
# (``client.get_sandbox_instance``) and holds the handle, whose ``send_input``
# writes INTO the pty. Resize and kill go back through the client wrapper
# methods (``resize_pty`` / ``kill_pty``) — those work without the handle.
from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable

from daytona import PtySize

from pocketpaw_ee.cloud.daytona.client import DaytonaClient
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR

logger = logging.getLogger(__name__)

# Terminal output sink: forwards raw VM bytes onward (to the WS as a binary
# frame, or to an in-memory buffer in the smoke test).
OutputSink = Callable[[bytes], Awaitable[None]]

# Sane default terminal geometry when the client hasn't sent a resize yet.
DEFAULT_COLS = 80
DEFAULT_ROWS = 24


class PtyBridge:
    """One live bash PTY session in a Daytona VM, bridged to an async sink.

    Lifecycle: ``start`` opens the pty and begins streaming output to the sink;
    ``send_input`` writes keystrokes; ``resize`` changes the terminal geometry;
    ``close`` tears the session down. One bridge == one pty session == one
    WebSocket connection. The ``session_id`` MUST be unique per connection so a
    second socket for the same VM never collides with an existing session.
    """

    def __init__(
        self,
        client: DaytonaClient,
        sandbox_id: str,
        session_id: str,
        sink: OutputSink,
    ) -> None:
        self._client = client
        self._sandbox_id = sandbox_id
        self._session_id = session_id
        self._sink = sink
        self._handle = None
        self._closed = False

    async def _on_data(self, data: bytes) -> None:
        """SDK callback: forward one chunk of terminal output to the sink.

        Never lets a sink failure propagate back into the SDK's read loop — a
        broken socket must not crash the pty reader; the WS handler tears the
        bridge down on the next receive error instead.
        """
        if self._closed:
            return
        try:
            await self._sink(data)
        except Exception:  # noqa: BLE001 — a dead sink shouldn't kill the reader
            logger.debug("pty sink failed for session=%s; dropping chunk", self._session_id)

    async def start(self, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS) -> None:
        """Open the pty session and start streaming its output to the sink.

        Grabs the SDK sandbox instance and creates the pty session directly so
        we KEEP the handle (the client wrapper would discard it). Blocks until
        the underlying WebSocket to the pty is connected so the first
        ``send_input`` isn't dropped on a not-yet-open socket.
        """
        sb = await self._client.get_sandbox_instance(self._sandbox_id)
        self._handle = await sb.process.create_pty_session(
            self._session_id,
            self._on_data,
            cwd=WEBSANDBOX_WORKDIR,  # open the shell where the repo is cloned
            pty_size=PtySize(rows=rows, cols=cols),
        )
        # Wait until the pty WebSocket is live before we accept input. Guarded by
        # hasattr so a minimal fake handle in tests needn't implement it.
        waiter = getattr(self._handle, "wait_for_connection", None)
        if waiter is not None:
            await waiter()
        logger.info(
            "pty bridge started: sandbox=%s session=%s size=%dx%d",
            self._sandbox_id,
            self._session_id,
            cols,
            rows,
        )

    async def send_input(self, data: str | bytes) -> None:
        """Write keystrokes INTO the pty via the held handle."""
        if self._closed or self._handle is None:
            return
        await self._handle.send_input(data)

    async def resize(self, cols: int, rows: int) -> None:
        """Resize the pty. Routed through the client wrapper (no handle needed)."""
        if self._closed:
            return
        await self._client.resize_pty(self._sandbox_id, self._session_id, cols, rows)

    async def close(self) -> None:
        """Tear the pty session down. Idempotent and never raises.

        Kills the server-side session (so the shell process doesn't leak) and
        disconnects the local handle. Both are best-effort: teardown runs in a
        ``finally`` on the socket, and a disconnected VM must not turn cleanup
        into an error.
        """
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._client.kill_pty(self._sandbox_id, self._session_id)
        if self._handle is not None:
            disconnect = getattr(self._handle, "disconnect", None)
            if disconnect is not None:
                with contextlib.suppress(Exception):
                    await disconnect()
        logger.info("pty bridge closed: sandbox=%s session=%s", self._sandbox_id, self._session_id)


__all__ = ["DEFAULT_COLS", "DEFAULT_ROWS", "OutputSink", "PtyBridge"]
