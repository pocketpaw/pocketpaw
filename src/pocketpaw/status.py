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
        # Monotonic counter bumped on every state change; waiters compare
        # their last-seen value to detect changes without a race.
        self._version = 0
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

    def _notify(self) -> None:
        """Bump version and wake any waiters."""
        self._version += 1
        self._change_event.set()

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
            self._notify()

        elif etype == "thinking":
            s = self._sessions.get(session_key)
            if s and s.state != "thinking":
                s.state = "thinking"
                s.tool_name = None
                s.state_changed_at = now
                self._notify()

        elif etype == "tool_start":
            s = self._sessions.get(session_key)
            if s:
                s.state = "tool_running"
                s.tool_name = data.get("name") or data.get("tool")
                s.state_changed_at = now
                self._notify()

        elif etype == "tool_result":
            s = self._sessions.get(session_key)
            if s and s.state == "tool_running":
                s.state = "streaming"
                s.tool_name = None
                s.state_changed_at = now
                self._notify()

        elif etype == "ask_user_question":
            s = self._sessions.get(session_key)
            if s:
                s.state = "waiting_for_user"
                s.tool_name = None
                s.state_changed_at = now
                self._notify()

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
                self._notify()
                # Schedule cleanup after TTL
                try:
                    loop = asyncio.get_running_loop()
                    loop.call_later(
                        _ERROR_TTL,
                        lambda key=session_key: self._sessions.pop(key, None),
                    )
                except RuntimeError:
                    pass  # No running loop (e.g. in tests)

        elif etype == "agent_end":
            self._sessions.pop(session_key, None)
            self._notify()

    # ── Snapshot ────────────────────────────────────────────────────────

    def _get_session_titles(self) -> dict[str, str]:
        """Load session titles from memory (best-effort)."""
        try:
            from pocketpaw.memory import get_memory_manager

            mgr = get_memory_manager()
            store = mgr._store
            if hasattr(store, "rebuild_session_index"):
                index = store._load_session_index()
                return {k: v.get("title", "") for k, v in index.items() if v.get("title")}
        except Exception:
            pass
        return {}

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
                    "title": None,
                    "state": s.state,
                    "tool_name": s.tool_name,
                    "duration_seconds": round(now - s.state_changed_at, 1),
                    "token_usage": token_usage,
                    "error_message": s.error_message,
                }
            )

        # Enrich session titles from memory (best-effort)
        if sessions:
            titles = self._get_session_titles()
            for session in sessions:
                safe_key = session["session_key"].replace(":", "_")
                title = titles.get(safe_key)
                if title:
                    session["title"] = title

        return {
            "global": {
                "state": global_state,
                "active_sessions": len(self._sessions),
                "max_concurrent": self._max_concurrent,
                "uptime_seconds": int(now - self._start_time),
            },
            "sessions": sessions,
        }

    async def wait_for_change(self, since_version: int = -1, timeout: float = 30.0) -> bool:
        """Wait for a state change. Returns True if changed, False on timeout.

        Pass ``since_version`` (from a previous ``version`` read) to avoid
        the race between clearing the event and waiting on it.
        """
        # If the version already advanced past what the caller saw, return immediately.
        if since_version >= 0 and self._version > since_version:
            return True
        self._change_event.clear()
        # Re-check after clearing to avoid the clear/set race.
        if since_version >= 0 and self._version > since_version:
            return True
        try:
            await asyncio.wait_for(self._change_event.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    @property
    def version(self) -> int:
        """Monotonic counter incremented on every state change."""
        return self._version
