# Browser session management
# Changes: Initial creation with BrowserSession and BrowserSessionManager
#
# Manages browser sessions with lifecycle handling, idle cleanup,
# and singleton access pattern.
"""Browser session management."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .driver import BrowserDriver


@dataclass
class BrowserSession:
    """Represents an active browser session.

    Tracks the driver instance along with metadata for lifecycle management.
    """

    session_id: str
    driver: BrowserDriver
    _created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    _last_used_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @property
    def created_at(self) -> datetime:
        """When the session was created."""
        return self._created_at

    @property
    def last_used_at(self) -> datetime:
        """When the session was last used."""
        return self._last_used_at

    def touch(self) -> None:
        """Update last used timestamp."""
        self._last_used_at = datetime.now(tz=UTC)


class BrowserSessionManager:
    """Manages browser sessions with lifecycle handling.

    Provides session creation, reuse, and cleanup functionality.
    Sessions are identified by a session_id and can be reused
    across multiple tool invocations.

    Usage:
        manager = get_browser_session_manager()
        session = await manager.get_or_create("my-session")
        # Use session.driver for browser operations
        await manager.close_session("my-session")
    """

    def __init__(self) -> None:
        """Initialize the session manager."""
        self._sessions: dict[str, BrowserSession] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    async def _get_lock(self, session_id: str) -> asyncio.Lock:
        """Get or create a lock for a session ID."""
        async with self._global_lock:
            if session_id not in self._locks:
                self._locks[session_id] = asyncio.Lock()
            return self._locks[session_id]

    async def get_or_create(self, session_id: str, headless: bool = True) -> BrowserSession:
        """Get an existing session or create a new one.

        Args:
            session_id: Unique identifier for the session
            headless: Whether to run browser headless (default True)

        Returns:
            The browser session (existing or newly created)
        """
        lock = await self._get_lock(session_id)

        async with lock:
            # Check if we have an existing valid session
            if session_id in self._sessions:
                session = self._sessions[session_id]
                if session.driver.is_launched:
                    session.touch()
                    return session
                else:
                    # Driver was closed, clean up and create new
                    await session.driver.close()
                    del self._sessions[session_id]

            # Create new session
            driver = BrowserDriver(headless=headless)
            await driver.launch()

            session = BrowserSession(session_id=session_id, driver=driver)
            self._sessions[session_id] = session

            return session

    async def close_session(self, session_id: str) -> None:
        """Close and remove a session.

        Args:
            session_id: The session to close
        """
        if session_id not in self._sessions:
            return

        lock = await self._get_lock(session_id)
        async with lock:
            if session_id in self._sessions:
                session = self._sessions[session_id]
                await session.driver.close()
                del self._sessions[session_id]

    async def cleanup_idle(self, timeout_seconds: int = 300) -> int:
        """Close sessions that have been idle longer than timeout.

        Args:
            timeout_seconds: How long a session can be idle (default 5 minutes)

        Returns:
            Number of sessions closed
        """
        now = datetime.now(tz=UTC)
        sessions_to_close = []

        # Find idle sessions
        for session_id, session in list(self._sessions.items()):
            idle_time = (now - session.last_used_at).total_seconds()
            if idle_time > timeout_seconds:
                sessions_to_close.append(session_id)

        # Close them
        for session_id in sessions_to_close:
            await self.close_session(session_id)

        return len(sessions_to_close)

    async def close_all(self) -> None:
        """Close all sessions."""
        session_ids = list(self._sessions.keys())
        for session_id in session_ids:
            await self.close_session(session_id)

    def list_sessions(self) -> list[str]:
        """Get list of active session IDs."""
        return list(self._sessions.keys())

    def has_session(self, session_id: str) -> bool:
        """Check if a session exists."""
        return session_id in self._sessions


# Singleton instance
_manager_instance: BrowserSessionManager | None = None
_manager_lock = asyncio.Lock()


def get_browser_session_manager() -> BrowserSessionManager:
    """Get the singleton session manager instance.

    Returns:
        The global BrowserSessionManager instance
    """
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = BrowserSessionManager()

        from pocketclaw.lifecycle import register

        def _reset():
            global _manager_instance
            _manager_instance = None

        register("browser_sessions", shutdown=_manager_instance.close_all, reset=_reset)
    return _manager_instance


__all__ = [
    "BrowserSession",
    "BrowserSessionManager",
    "get_browser_session_manager",
]
