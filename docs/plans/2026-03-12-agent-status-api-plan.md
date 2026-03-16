# Agent Status API & CLI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Expose real-time agent state (idle/thinking/tool_running/streaming/error) via a public REST endpoint, SSE stream, and CLI command for external integrations (stream decks, LED indicators, desktop widgets).

**Architecture:** A `StatusTracker` subscribes to the message bus and maintains per-session state in memory. A FastAPI router exposes the state via polling (`GET /api/v1/agent/status`) and SSE (`GET /api/v1/agent/status/stream`). Auth is an optional static API key (`POCKETPAW_STATUS_API_KEY`). The CLI command (`pocketpaw status`) hits the REST endpoint locally.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, asyncio, SSE (StreamingResponse), argparse, httpx (CLI client)

---

### Task 1: Add `status_api_key` to config

**Files:**
- Modify: `src/pocketpaw/config.py:664` (after `owner_id` field)

**Step 1: Add the field**

In `src/pocketpaw/config.py`, add after the `owner_id` field (line 664):

```python
    # Status API
    status_api_key: str = Field(
        default="",
        description="Optional API key for the agent status endpoint. Leave empty to skip auth.",
    )
```

**Step 2: Verify it loads**

Run: `uv run python -c "from pocketpaw.config import Settings; s = Settings.load(); print(s.status_api_key)"`
Expected: empty string (no error)

**Step 3: Commit**

```bash
git add src/pocketpaw/config.py
git commit -m "feat(config): add status_api_key setting"
```

---

### Task 2: Create response schemas

**Files:**
- Create: `src/pocketpaw/api/v1/schemas/status.py`

**Step 1: Write the schemas**

Create `src/pocketpaw/api/v1/schemas/status.py`:

```python
# Agent status response schemas.
# Created: 2026-03-12

from __future__ import annotations

from pydantic import BaseModel


class SessionStatus(BaseModel):
    """Status of a single active agent session."""

    session_key: str
    session_id: str
    channel: str
    title: str | None = None
    state: str  # thinking, tool_running, streaming, waiting_for_user, error
    tool_name: str | None = None
    duration_seconds: float = 0
    token_usage: dict[str, int] | None = None
    error_message: str | None = None


class GlobalStatus(BaseModel):
    """Global agent status."""

    state: str  # idle, active, degraded
    active_sessions: int = 0
    max_concurrent: int = 5
    uptime_seconds: int = 0


class AgentStatusResponse(BaseModel):
    """Full agent status response."""

    global_status: GlobalStatus
    sessions: list[SessionStatus] = []
```

**Step 2: Verify import**

Run: `uv run python -c "from pocketpaw.api.v1.schemas.status import AgentStatusResponse; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add src/pocketpaw/api/v1/schemas/status.py
git commit -m "feat(schemas): add agent status response models"
```

---

### Task 3: Create StatusTracker (bus subscriber)

**Files:**
- Create: `src/pocketpaw/status.py`

This is the core component. It subscribes to bus events and maintains per-session state.

**Step 1: Write the tracker**

Create `src/pocketpaw/status.py`:

```python
# StatusTracker — maintains real-time per-session agent state.
# Created: 2026-03-12

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from pocketpaw.bus.events import SystemEvent

logger = logging.getLogger(__name__)

# How long an error state persists before cleanup (seconds)
_ERROR_TTL = 30.0


@dataclass
class _SessionState:
    """Internal mutable state for a single session."""

    session_key: str
    state: str = "thinking"  # thinking, tool_running, streaming, waiting_for_user, error
    tool_name: str | None = None
    error_message: str | None = None
    started_at: float = field(default_factory=time.monotonic)
    state_changed_at: float = field(default_factory=time.monotonic)
    token_input: int = 0
    token_output: int = 0


class StatusTracker:
    """Subscribes to the message bus and tracks per-session agent state.

    Call ``subscribe()`` once after the bus is available. Query current
    state via ``snapshot()``.
    """

    def __init__(self, max_concurrent: int = 5) -> None:
        self._sessions: dict[str, _SessionState] = {}
        self._max_concurrent = max_concurrent
        self._start_time = time.monotonic()
        self._subscribed = False
        self._change_event = asyncio.Event()

    # ── Bus wiring ──────────────────────────────────────────────────────

    async def subscribe(self) -> None:
        """Subscribe to the message bus for system events."""
        if self._subscribed:
            return
        from pocketpaw.bus import get_message_bus

        bus = get_message_bus()
        bus.subscribe_system(self._on_event)
        self._subscribed = True
        logger.info("StatusTracker subscribed to message bus")

    async def unsubscribe(self) -> None:
        """Unsubscribe from the message bus."""
        if not self._subscribed:
            return
        from pocketpaw.bus import get_message_bus

        bus = get_message_bus()
        bus.unsubscribe_system(self._on_event)
        self._subscribed = False

    # ── Event handler ───────────────────────────────────────────────────

    async def _on_event(self, evt: SystemEvent) -> None:
        """Process a system event and update session state."""
        data = evt.data or {}
        session_key = data.get("session_key", "")
        if not session_key:
            return

        now = time.monotonic()
        etype = evt.event_type

        if etype == "agent_start":
            self._sessions[session_key] = _SessionState(
                session_key=session_key, started_at=now, state_changed_at=now
            )
            self._change_event.set()

        elif etype == "thinking":
            s = self._sessions.get(session_key)
            if s:
                s.state = "thinking"
                s.tool_name = None
                s.state_changed_at = now
                self._change_event.set()

        elif etype == "tool_start":
            s = self._sessions.get(session_key)
            if s:
                s.state = "tool_running"
                s.tool_name = data.get("name") or data.get("tool")
                s.state_changed_at = now
                self._change_event.set()

        elif etype == "tool_result":
            s = self._sessions.get(session_key)
            if s and s.state == "tool_running":
                s.state = "streaming"
                s.tool_name = None
                s.state_changed_at = now
                self._change_event.set()

        elif etype == "ask_user_question":
            s = self._sessions.get(session_key)
            if s:
                s.state = "waiting_for_user"
                s.tool_name = None
                s.state_changed_at = now
                self._change_event.set()

        elif etype == "token_usage":
            s = self._sessions.get(session_key)
            if s:
                s.token_input += data.get("input", 0)
                s.token_output += data.get("output", 0)

        elif etype == "error":
            s = self._sessions.get(session_key)
            if s:
                s.state = "error"
                s.error_message = data.get("message", "Unknown error")
                s.tool_name = None
                s.state_changed_at = now
                self._change_event.set()
                # Schedule cleanup after TTL
                asyncio.get_event_loop().call_later(
                    _ERROR_TTL,
                    lambda key=session_key: self._sessions.pop(key, None),
                )

        elif etype == "agent_end":
            self._sessions.pop(session_key, None)
            self._change_event.set()

    # ── Snapshot ────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Return the current status as a JSON-serializable dict."""
        now = time.monotonic()
        has_error = any(s.state == "error" for s in self._sessions.values())

        if not self._sessions:
            global_state = "idle"
        elif has_error:
            global_state = "degraded"
        else:
            global_state = "active"

        sessions = []
        for s in self._sessions.values():
            channel, _, sid = s.session_key.partition(":")
            token_usage = None
            if s.token_input or s.token_output:
                token_usage = {"input": s.token_input, "output": s.token_output}
            sessions.append(
                {
                    "session_key": s.session_key,
                    "session_id": sid or s.session_key,
                    "channel": channel or "unknown",
                    "title": None,  # filled by endpoint if memory is available
                    "state": s.state,
                    "tool_name": s.tool_name,
                    "duration_seconds": round(now - s.state_changed_at, 1),
                    "token_usage": token_usage,
                    "error_message": s.error_message,
                }
            )

        return {
            "global": {
                "state": global_state,
                "active_sessions": len(self._sessions),
                "max_concurrent": self._max_concurrent,
                "uptime_seconds": int(now - self._start_time),
            },
            "sessions": sessions,
        }

    async def wait_for_change(self, timeout: float = 30.0) -> bool:
        """Wait for a state change. Returns True if changed, False on timeout."""
        self._change_event.clear()
        try:
            await asyncio.wait_for(self._change_event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False
```

**Step 2: Write the test**

Create `tests/test_status_tracker.py`:

```python
"""Tests for StatusTracker."""

import asyncio

import pytest

from pocketpaw.bus.events import SystemEvent
from pocketpaw.status import StatusTracker


@pytest.fixture
def tracker():
    return StatusTracker(max_concurrent=3)


class TestStatusTracker:
    async def test_idle_by_default(self, tracker):
        snap = tracker.snapshot()
        assert snap["global"]["state"] == "idle"
        assert snap["global"]["active_sessions"] == 0
        assert snap["sessions"] == []

    async def test_agent_start_creates_session(self, tracker):
        await tracker._on_event(
            SystemEvent(event_type="agent_start", data={"session_key": "websocket:abc"})
        )
        snap = tracker.snapshot()
        assert snap["global"]["state"] == "active"
        assert snap["global"]["active_sessions"] == 1
        assert snap["sessions"][0]["session_key"] == "websocket:abc"
        assert snap["sessions"][0]["channel"] == "websocket"
        assert snap["sessions"][0]["session_id"] == "abc"

    async def test_thinking_state(self, tracker):
        await tracker._on_event(
            SystemEvent(event_type="agent_start", data={"session_key": "ws:1"})
        )
        await tracker._on_event(
            SystemEvent(event_type="thinking", data={"session_key": "ws:1"})
        )
        snap = tracker.snapshot()
        assert snap["sessions"][0]["state"] == "thinking"

    async def test_tool_running_state(self, tracker):
        await tracker._on_event(
            SystemEvent(event_type="agent_start", data={"session_key": "ws:1"})
        )
        await tracker._on_event(
            SystemEvent(
                event_type="tool_start",
                data={"session_key": "ws:1", "name": "bash"},
            )
        )
        snap = tracker.snapshot()
        assert snap["sessions"][0]["state"] == "tool_running"
        assert snap["sessions"][0]["tool_name"] == "bash"

    async def test_tool_result_transitions_to_streaming(self, tracker):
        await tracker._on_event(
            SystemEvent(event_type="agent_start", data={"session_key": "ws:1"})
        )
        await tracker._on_event(
            SystemEvent(event_type="tool_start", data={"session_key": "ws:1", "name": "bash"})
        )
        await tracker._on_event(
            SystemEvent(event_type="tool_result", data={"session_key": "ws:1"})
        )
        snap = tracker.snapshot()
        assert snap["sessions"][0]["state"] == "streaming"
        assert snap["sessions"][0]["tool_name"] is None

    async def test_error_state_sets_degraded(self, tracker):
        await tracker._on_event(
            SystemEvent(event_type="agent_start", data={"session_key": "ws:1"})
        )
        await tracker._on_event(
            SystemEvent(
                event_type="error",
                data={"session_key": "ws:1", "message": "Rate limit"},
            )
        )
        snap = tracker.snapshot()
        assert snap["global"]["state"] == "degraded"
        assert snap["sessions"][0]["state"] == "error"
        assert snap["sessions"][0]["error_message"] == "Rate limit"

    async def test_agent_end_removes_session(self, tracker):
        await tracker._on_event(
            SystemEvent(event_type="agent_start", data={"session_key": "ws:1"})
        )
        await tracker._on_event(
            SystemEvent(event_type="agent_end", data={"session_key": "ws:1"})
        )
        snap = tracker.snapshot()
        assert snap["global"]["state"] == "idle"
        assert snap["sessions"] == []

    async def test_waiting_for_user_state(self, tracker):
        await tracker._on_event(
            SystemEvent(event_type="agent_start", data={"session_key": "ws:1"})
        )
        await tracker._on_event(
            SystemEvent(event_type="ask_user_question", data={"session_key": "ws:1"})
        )
        snap = tracker.snapshot()
        assert snap["sessions"][0]["state"] == "waiting_for_user"

    async def test_token_usage_accumulates(self, tracker):
        await tracker._on_event(
            SystemEvent(event_type="agent_start", data={"session_key": "ws:1"})
        )
        await tracker._on_event(
            SystemEvent(
                event_type="token_usage",
                data={"session_key": "ws:1", "input": 100, "output": 50},
            )
        )
        await tracker._on_event(
            SystemEvent(
                event_type="token_usage",
                data={"session_key": "ws:1", "input": 200, "output": 80},
            )
        )
        snap = tracker.snapshot()
        assert snap["sessions"][0]["token_usage"] == {"input": 300, "output": 130}

    async def test_max_concurrent_in_snapshot(self, tracker):
        snap = tracker.snapshot()
        assert snap["global"]["max_concurrent"] == 3

    async def test_ignores_events_without_session_key(self, tracker):
        await tracker._on_event(SystemEvent(event_type="thinking", data={}))
        snap = tracker.snapshot()
        assert snap["global"]["state"] == "idle"
```

**Step 3: Run tests**

Run: `uv run pytest tests/test_status_tracker.py -v`
Expected: All tests pass

**Step 4: Commit**

```bash
git add src/pocketpaw/status.py tests/test_status_tracker.py
git commit -m "feat: add StatusTracker bus subscriber for agent state"
```

---

### Task 4: Create the API endpoint and SSE stream

**Files:**
- Create: `src/pocketpaw/api/v1/agent_status.py`
- Modify: `src/pocketpaw/api/v1/__init__.py:42` (add router to `_V1_ROUTERS`)
- Modify: `src/pocketpaw/dashboard_state.py:24` (instantiate StatusTracker)

**Step 1: Write the router**

Create `src/pocketpaw/api/v1/agent_status.py`:

```python
# Agent status API — polling and SSE stream for external integrations.
# Created: 2026-03-12

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Status"])

_DEBOUNCE_MS = 200


def _check_status_key(request: Request, key: str | None = None):
    """Validate optional status API key from header or query param."""
    from pocketpaw.config import Settings

    settings = Settings.load()
    expected = settings.status_api_key
    if not expected:
        return  # No key configured, allow all

    provided = (
        key
        or request.headers.get("x-status-key", "")
    )
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing status API key")


@router.get("/agent/status")
async def get_agent_status(request: Request, key: str | None = Query(None)):
    """Return current agent state: global status and per-session breakdown.

    Authenticate with ``X-Status-Key`` header or ``?key=`` query param
    if ``POCKETPAW_STATUS_API_KEY`` is set.
    """
    _check_status_key(request, key)

    from pocketpaw.dashboard_state import status_tracker

    return status_tracker.snapshot()


@router.get("/agent/status/stream")
async def agent_status_stream(request: Request, key: str | None = Query(None)):
    """SSE stream of agent state changes.

    Sends a full snapshot on connect and on every state change.
    Debounced at 200ms to avoid flooding during rapid tool sequences.
    """
    _check_status_key(request, key)

    from pocketpaw.dashboard_state import status_tracker

    async def _event_generator():
        try:
            # Send initial snapshot immediately
            snap = status_tracker.snapshot()
            yield f"event: status\ndata: {json.dumps(snap)}\n\n"

            while True:
                changed = await status_tracker.wait_for_change(timeout=30.0)
                if changed:
                    # Debounce: wait a bit for rapid successive events to settle
                    await asyncio.sleep(_DEBOUNCE_MS / 1000)
                    snap = status_tracker.snapshot()
                    yield f"event: status\ndata: {json.dumps(snap)}\n\n"
                else:
                    # Keepalive every 30s
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
```

**Step 2: Register the router**

In `src/pocketpaw/api/v1/__init__.py`, add to `_V1_ROUTERS` list (after the Metrics entry at line 42):

```python
    ("pocketpaw.api.v1.agent_status", "router", "Status"),
```

**Step 3: Add StatusTracker to dashboard_state**

In `src/pocketpaw/dashboard_state.py`, add the import (after line 10):

```python
from pocketpaw.status import StatusTracker
```

And add the singleton (after line 24, after `agent_loop = AgentLoop()`):

```python
status_tracker = StatusTracker()
```

**Step 4: Subscribe StatusTracker on startup**

The tracker needs to subscribe to the bus after the dashboard starts. In `src/pocketpaw/dashboard_state.py`, there is likely a startup hook. If not, the subscription should happen in the dashboard startup code.

Search for where `agent_loop.start()` is called, and add `await status_tracker.subscribe()` right after it. This is likely in `src/pocketpaw/dashboard.py` or `src/pocketpaw/api/serve.py`.

Look for the pattern:
```python
await agent_loop.start()
# Add right after:
await status_tracker.subscribe()
```

Also update max_concurrent from settings:
```python
status_tracker._max_concurrent = settings.max_concurrent_conversations
```

**Step 5: Verify endpoint works**

Start the server, then:
Run: `curl http://localhost:8888/api/v1/agent/status`
Expected: JSON with `global.state: "idle"` and empty `sessions` array

**Step 6: Commit**

```bash
git add src/pocketpaw/api/v1/agent_status.py src/pocketpaw/api/v1/__init__.py src/pocketpaw/dashboard_state.py
git commit -m "feat(api): add GET /api/v1/agent/status and SSE stream"
```

---

### Task 5: Add CLI command

**Files:**
- Create: `src/pocketpaw/cli/__init__.py` (if not exists)
- Create: `src/pocketpaw/cli/status.py`
- Modify: `src/pocketpaw/__main__.py` (add `status` subcommand)

**Step 1: Check if cli/ directory exists**

Run: `ls src/pocketpaw/cli/ 2>/dev/null || echo "needs creation"`

**Step 2: Write the CLI status module**

Create `src/pocketpaw/cli/status.py`:

```python
# CLI status command — queries the agent status API and prints results.
# Created: 2026-03-12

from __future__ import annotations

import json
import os
import sys
import time


def _get_base_url(port: int) -> str:
    return f"http://localhost:{port}"


def _get_status(port: int) -> dict | None:
    """Fetch agent status from the local API."""
    import httpx

    url = f"{_get_base_url(port)}/api/v1/agent/status"
    headers = {}
    key = os.environ.get("POCKETPAW_STATUS_API_KEY", "")
    if key:
        headers["X-Status-Key"] = key

    try:
        resp = httpx.get(url, headers=headers, timeout=5.0)
        resp.raise_for_status()
        return resp.json()
    except httpx.ConnectError:
        return None
    except httpx.HTTPStatusError as e:
        print(f"Error: {e.response.status_code} — {e.response.text}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return None


def _format_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def _print_table(data: dict) -> None:
    """Print human-readable status table."""
    g = data["global"]
    sessions = data["sessions"]

    state_display = g["state"].upper()
    print()
    print("PocketPaw Status")
    print(f"  State:    {state_display}")
    print(f"  Sessions: {g['active_sessions']} / {g['max_concurrent']}")
    print(f"  Uptime:   {_format_duration(g['uptime_seconds'])}")

    if sessions:
        print()
        print("Active Sessions")
        # Header
        print(f"  {'SESSION':<20} {'CHANNEL':<12} {'STATE':<18} {'TOOL':<12} {'DURATION'}")
        for s in sessions:
            title = (s.get("title") or s["session_id"])[:18]
            tool = s.get("tool_name") or "-"
            dur = _format_duration(s["duration_seconds"])
            state = s["state"]
            if state == "error":
                state = f"error: {s.get('error_message', '')}"[:18]
            print(f"  {title:<20} {s['channel']:<12} {state:<18} {tool:<12} {dur}")
    print()


def run_status(port: int = 8888, as_json: bool = False, watch: float = 0) -> int:
    """Run the status command. Returns exit code."""
    if watch > 0:
        return _run_watch(port, as_json, watch)

    data = _get_status(port)
    if data is None:
        print("PocketPaw is not running (could not connect to localhost:{})".format(port))
        return 1

    if as_json:
        print(json.dumps(data, indent=2))
    else:
        _print_table(data)
    return 0


def _run_watch(port: int, as_json: bool, interval: float) -> int:
    """Poll and redraw status at interval."""
    try:
        while True:
            # Clear screen
            print("\033[2J\033[H", end="")
            data = _get_status(port)
            if data is None:
                print("PocketPaw is not running (could not connect to localhost:{})".format(port))
            elif as_json:
                print(json.dumps(data, indent=2))
            else:
                _print_table(data)
            print(f"[Refreshing every {interval}s — Ctrl+C to stop]")
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
```

**Step 3: Add CLI args to __main__.py**

In `src/pocketpaw/__main__.py`, add the `command` argument description (around line 90, update the epilog):

```
  pocketpaw status                    Show agent status
  pocketpaw status --json             Show agent status as JSON
  pocketpaw status --watch            Monitor status (refresh every 2s)
```

Then add these arguments (after the `--version` argument, around line 168):

```python
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output status as JSON (used with 'status' command)",
    )
    parser.add_argument(
        "--watch",
        nargs="?",
        type=float,
        const=2.0,
        default=0,
        help="Watch mode: refresh status every N seconds (default: 2)",
    )
```

Then add the handler in the command dispatch (after line 249, before `elif args.check_ollama`):

```python
        if args.command == "serve":
            # ... existing serve code ...
        elif args.command == "status":
            from pocketpaw.cli.status import run_status

            exit_code = run_status(
                port=args.port,
                as_json=args.json,
                watch=args.watch,
            )
            raise SystemExit(exit_code)
```

**Step 4: Test the CLI**

Start the server in one terminal, then:
Run: `uv run pocketpaw status`
Expected: Status table showing idle state

Run: `uv run pocketpaw status --json`
Expected: JSON output

**Step 5: Commit**

```bash
git add src/pocketpaw/cli/ src/pocketpaw/__main__.py
git commit -m "feat(cli): add pocketpaw status command with --json and --watch"
```

---

### Task 6: Wire up StatusTracker subscription on startup

**Files:**
- Modify: whichever file calls `agent_loop.start()` (likely `src/pocketpaw/dashboard.py` or `src/pocketpaw/api/serve.py`)

**Step 1: Find the startup hook**

Run: `grep -rn "agent_loop.start\|agent_loop\.start" src/pocketpaw/`

**Step 2: Add subscription**

After `await agent_loop.start()`, add:

```python
from pocketpaw.dashboard_state import status_tracker
await status_tracker.subscribe()
```

Also set max_concurrent from settings:
```python
status_tracker._max_concurrent = settings.max_concurrent_conversations
```

**Step 3: Find the shutdown hook**

Run: `grep -rn "agent_loop.stop\|shutdown" src/pocketpaw/lifecycle.py`

Add `await status_tracker.unsubscribe()` in the shutdown sequence.

**Step 4: Commit**

```bash
git add src/pocketpaw/dashboard.py  # or wherever the change was
git commit -m "feat: wire StatusTracker subscribe/unsubscribe to lifecycle"
```

---

### Task 7: Enrich session titles from memory

**Files:**
- Modify: `src/pocketpaw/status.py` (add optional title lookup)

**Step 1: Add title enrichment to snapshot()**

In `StatusTracker.snapshot()`, after building the sessions list, optionally look up session titles from the memory manager:

```python
# Try to enrich session titles from memory
try:
    from pocketpaw.memory import get_memory_manager

    mgr = get_memory_manager()
    index = mgr._store._load_session_index()
    for session in sessions:
        safe_key = session["session_key"].replace(":", "_")
        meta = index.get(safe_key, {})
        if meta.get("title"):
            session["title"] = meta["title"]
except Exception:
    pass  # Title enrichment is best-effort
```

**Step 2: Commit**

```bash
git add src/pocketpaw/status.py
git commit -m "feat: enrich status sessions with titles from memory"
```

---

### Task 8: Integration test

**Files:**
- Create: `tests/test_status_api.py`

**Step 1: Write integration test**

```python
"""Integration tests for agent status API endpoint."""

import pytest
from unittest.mock import patch, MagicMock


class TestAgentStatusEndpoint:
    """Test the status endpoint auth and response shape."""

    def test_snapshot_shape(self):
        """Verify snapshot dict has correct structure."""
        from pocketpaw.status import StatusTracker

        tracker = StatusTracker(max_concurrent=5)
        snap = tracker.snapshot()

        assert "global" in snap
        assert "sessions" in snap
        assert snap["global"]["state"] == "idle"
        assert snap["global"]["max_concurrent"] == 5
        assert isinstance(snap["sessions"], list)

    async def test_status_key_rejects_bad_key(self):
        """Verify auth check rejects wrong key."""
        from fastapi import HTTPException
        from pocketpaw.api.v1.agent_status import _check_status_key

        mock_request = MagicMock()
        mock_request.headers = {"x-status-key": "wrong"}

        with patch("pocketpaw.api.v1.agent_status.Settings") as MockSettings:
            MockSettings.load.return_value = MagicMock(status_api_key="correct-key")
            with pytest.raises(HTTPException) as exc_info:
                _check_status_key(mock_request, None)
            assert exc_info.value.status_code == 403

    async def test_status_key_allows_when_no_key_configured(self):
        """Verify auth is skipped when no key is set."""
        from pocketpaw.api.v1.agent_status import _check_status_key

        mock_request = MagicMock()
        mock_request.headers = {}

        with patch("pocketpaw.api.v1.agent_status.Settings") as MockSettings:
            MockSettings.load.return_value = MagicMock(status_api_key="")
            _check_status_key(mock_request, None)  # Should not raise
```

**Step 2: Run tests**

Run: `uv run pytest tests/test_status_tracker.py tests/test_status_api.py -v`
Expected: All tests pass

**Step 3: Commit**

```bash
git add tests/test_status_api.py
git commit -m "test: add integration tests for agent status API"
```

---

### Task 9: Final lint and PR

**Step 1: Run linter**

Run: `.venv/Scripts/ruff.exe check src/pocketpaw/status.py src/pocketpaw/api/v1/agent_status.py src/pocketpaw/cli/status.py`
Run: `.venv/Scripts/ruff.exe format src/pocketpaw/status.py src/pocketpaw/api/v1/agent_status.py src/pocketpaw/cli/status.py`

Fix any issues.

**Step 2: Run all tests**

Run: `uv run pytest tests/test_status_tracker.py tests/test_status_api.py -v`
Expected: All pass

**Step 3: Create branch and PR**

```bash
git checkout -b feat/agent-status-api
git push -u origin feat/agent-status-api
gh pr create --base dev --title "feat: agent status API, SSE stream, and CLI command" --body "..."
```
