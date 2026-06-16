"""Terminal router — SSE-backed PTY shell for the /code IDE.

Exposes three endpoints:
  • GET  /api/v1/terminal/sse    — SSE stream of terminal output (stdout + stderr)
  • POST /api/v1/terminal/input   — send keystrokes / input to the shell
  • POST /api/v1/terminal/resize  — resize the terminal (cols, rows)

A single background shell process (bash) is created on first SSE connect and
persists for the lifetime of the server. Keystrokes are written to its stdin;
output is read from its PTY and broadcast to all connected SSE clients via
an in-memory publish/subscribe pattern.

Created: 2026-06-16
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import pty
import select
import signal
import struct
import termios

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Terminal"])

# ---------------------------------------------------------------------------
# Singleton shell process — one PTY-backed bash per server lifetime.
# ---------------------------------------------------------------------------


class ShellProcess:
    """Manages a single PTY-backed bash process.

    Output from the PTY is continuously read and pushed to all connected
    SSE clients via an in-memory async broadcast queue.
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._master_fd: int | None = None  # PTY master file descriptor
        self._subscribers: list[asyncio.Queue[bytes]] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._started = False
        # Circular buffer of recent output — replayed to new subscribers so
        # they always see the current prompt and terminal state. Capped at
        # ~256 KB (256 × 1024 byte chunks).
        self._buf: list[bytes] = []
        self._buf_max = 256
        self._buf_size = 0

    async def ensure_running(self) -> None:
        """Start the shell if not already running."""
        if self._started and self._proc is not None and self._proc.returncode is None:
            return
        async with self._lock:
            if self._started:
                return
            self._started = True

        # Create a PTY pair.
        master_fd, slave_fd = pty.openpty()

        # Set the initial window size.
        s = struct.pack("HHHH", 24, 80, 0, 0)
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, s)

        # Set the slave to raw mode.
        attrs = termios.tcgetattr(slave_fd)
        # Enable OPOST + ONLCR so the kernel converts \n to \r\n on output.
        attrs[1] = attrs[1] | (termios.OPOST | termios.ONLCR)
        # Disable ICANON so bash receives each keystroke immediately.
        # ECHO is OFF — the frontend handles local echo for instant feedback.
        # ISIG stays on so Ctrl+C/Ctrl+Z work.
        attrs[3] = attrs[3] & ~(
            termios.ICANON | termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL
        )
        attrs[3] = attrs[3] | termios.ISIG
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

        # Set the master to non-blocking.
        fl = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

        self._master_fd = master_fd

        # Build env with TERM set so programs (vim, htop, etc.) work properly.
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["SHELL"] = "/bin/bash"
        env["LANG"] = "C.UTF-8"

        # Start bash with the slave as its stdin/stdout/stderr.
        self._proc = await asyncio.create_subprocess_exec(
            "bash",
            "--login",
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            preexec_fn=lambda: os.setsid(),
            env=env,
        )

        # Close the slave end in the parent — only the child needs it.
        os.close(slave_fd)

        logger.info("Terminal shell started (pid=%s)", self._proc.pid)

        # Start the reader task that pumps PTY output to subscribers.
        self._task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self) -> None:
        """Read from the PTY master and broadcast to all subscribers."""
        loop = asyncio.get_event_loop()
        fd = self._master_fd
        buf = b""

        try:
            while True:
                # Use asyncio to wait for the fd to be readable.
                await loop.run_in_executor(None, select.select, [fd], [], [fd])

                try:
                    data = os.read(fd, 4096)
                except BlockingIOError:
                    await asyncio.sleep(0.01)
                    continue
                except OSError as e:
                    logger.warning("Terminal read error: %s", e)
                    break

                if not data:
                    # EOF — shell exited.
                    logger.info(
                        "Terminal shell exited (pid=%s)", self._proc.pid if self._proc else "?"
                    )
                    break

                # Accumulate and try to decode as UTF-8.
                buf += data
                try:
                    text = buf.decode("utf-8")
                    buf = b""
                except UnicodeDecodeError:
                    # Partial UTF-8 character — wait for more data.
                    if len(buf) > 4:
                        # Force-decode with replacement if buffer is too large.
                        text = buf.decode("utf-8", errors="replace")
                        buf = b""
                    else:
                        text = None

                if text:
                    self._broadcast(text.encode("utf-8"))

        except asyncio.CancelledError:
            pass
        finally:
            await self._cleanup()

    def _broadcast(self, data: bytes) -> None:
        """Push data to all subscriber queues and buffer for replay."""
        # Maintain a bounded circular buffer (max _buf_max chunks).
        self._buf.append(data)
        self._buf_size += len(data)
        while len(self._buf) > self._buf_max:
            evicted = self._buf.pop(0)
            self._buf_size -= len(evicted)

        dead: list[asyncio.Queue[bytes]] = []
        for q in self._subscribers:
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.remove(q)

    async def write(self, data: str | bytes) -> None:
        """Write data to the PTY master (the shell's stdin)."""
        if self._master_fd is None or (self._proc and self._proc.returncode is not None):
            self._started = False
            await self.ensure_running()

        if self._master_fd is not None:
            if isinstance(data, str):
                data = data.encode("utf-8", errors="replace")
            try:
                os.write(self._master_fd, data)
            except OSError as e:
                logger.warning("Terminal write error: %s", e)

    async def resize(self, cols: int, rows: int) -> None:
        """Resize the terminal window."""
        if self._master_fd is not None:
            try:
                s = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, s)
            except (OSError, struct.error) as e:
                logger.warning("Terminal resize error: %s", e)

    async def subscribe(self) -> asyncio.Queue[bytes]:
        """Register a subscriber queue and return it, replaying buffered
        output so the new subscriber sees the current terminal state."""
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
        # Replay buffered output so the subscriber sees the current prompt.
        for chunk in self._buf:
            try:
                q.put_nowait(chunk)
            except asyncio.QueueFull:
                break
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[bytes]) -> None:
        """Remove a subscriber queue."""
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def _cleanup(self) -> None:
        """Kill the shell and close the PTY."""
        if self._proc and self._proc.returncode is None:
            try:
                # Try SIGTERM first for a clean shutdown.
                self._proc.send_signal(signal.SIGTERM)
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                except TimeoutError:
                    self._proc.send_signal(signal.SIGKILL)
                    await self._proc.wait()
            except ProcessLookupError:
                pass
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
        self._master_fd = None
        self._proc = None
        self._task = None


# Singleton instance.
_shell = ShellProcess()


# ---------------------------------------------------------------------------
# Helper: register a shutdown handler so the shell is cleaned up when the
# server stops.
# ---------------------------------------------------------------------------


def _register_shutdown(app: object) -> None:
    """Register the shell cleanup as a FastAPI shutdown event."""
    try:
        from fastapi import FastAPI

        if isinstance(app, FastAPI):

            async def _shutdown_shell() -> None:
                logger.info("Shutting down terminal shell...")
                await _shell._cleanup()

            app.add_event_handler("shutdown", _shutdown_shell)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------


async def _sse_generator():
    """Generator that yields SSE events from the terminal output."""
    await _shell.ensure_running()
    queue = await _shell.subscribe()

    try:
        # Send an initial "connected" event so the client knows the stream
        # is live. The shell's prompt will follow as data events.
        yield "event: connected\ndata: {}\n\n"

        while True:
            try:
                data = await asyncio.wait_for(queue.get(), timeout=30.0)
            except TimeoutError:
                # Send a keepalive comment to prevent proxies from closing
                # idle connections.
                yield ": keepalive\n\n"
                continue

            if not data:
                break

            # Decode to text. Errors already handled in the reader loop,
            # but guard here too.
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")

            # Send the raw data as a single SSE event so ANSI sequences and
            # cursor positioning arrive intact. Do NOT split on newlines —
            # xterm.js reassembles partial writes correctly.
            #
            # Escape the data field per the SSE spec: replace \n with
            # \ndata: so multi-line output is valid SSE.
            for line in text.split("\n"):
                if line:
                    yield f"data: {line}\n"
                else:
                    yield "data: \n"
            yield "\n"

            # Small yield to let other coroutines run.
            await asyncio.sleep(0)
    except asyncio.CancelledError:
        pass
    finally:
        _shell.unsubscribe(queue)


@router.get("/terminal/sse")
async def terminal_sse():
    """SSE endpoint: streams terminal output to the client.

    The client opens an EventSource to this URL and receives ``data`` events
    containing the raw terminal output (stdout + stderr combined). ANSI escape
    sequences are preserved — xterm.js on the client renders them.
    """
    return StreamingResponse(
        _sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Input endpoint
# ---------------------------------------------------------------------------


class TerminalInput(BaseModel):
    data: str


@router.post("/terminal/input")
async def terminal_input(body: TerminalInput):
    """POST endpoint: send keystrokes/input to the terminal.

    The body should be a JSON object with a ``data`` field containing the
    characters to send (e.g. ``{"data": "ls -la\\n"}``). Backslash escapes
    in the JSON string are handled automatically by the JSON parser.
    """
    await _shell.ensure_running()
    await _shell.write(body.data)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Resize endpoint
# ---------------------------------------------------------------------------


class TerminalResize(BaseModel):
    cols: int
    rows: int


@router.post("/terminal/resize")
async def terminal_resize(body: TerminalResize):
    """POST endpoint: resize the terminal window.

    The body should be a JSON object with ``cols`` and ``rows`` fields
    (e.g. ``{"cols": 80, "rows": 24}``).
    """
    await _shell.resize(body.cols, body.rows)
    return {"ok": True}
