# Browser session management tests
# Changes: Initial creation with session manager tests
"""Tests for browser session management."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pocketclaw.browser.session import (
    BrowserSession,
    BrowserSessionManager,
    get_browser_session_manager,
)


class TestBrowserSession:
    """Tests for BrowserSession dataclass."""

    def test_create_session(self):
        """Should create a session with driver and metadata."""
        mock_driver = MagicMock()
        session = BrowserSession(session_id="test-123", driver=mock_driver)

        assert session.session_id == "test-123"
        assert session.driver is mock_driver
        assert session.created_at is not None
        assert session.last_used_at is not None

    def test_session_touch(self):
        """Should update last_used_at on touch."""
        mock_driver = MagicMock()
        session = BrowserSession(session_id="test-123", driver=mock_driver)

        original_time = session.last_used_at
        # Small sleep to ensure time difference
        import time

        time.sleep(0.01)

        session.touch()

        assert session.last_used_at > original_time


class TestBrowserSessionManager:
    """Tests for BrowserSessionManager class."""

    def test_manager_init(self):
        """Should initialize with empty sessions."""
        manager = BrowserSessionManager()
        assert manager._sessions == {}

    @pytest.mark.asyncio
    async def test_get_or_create_creates_new_session(self):
        """Should create new session if none exists."""
        manager = BrowserSessionManager()

        with patch("pocketclaw.browser.session.BrowserDriver") as MockDriver:
            mock_driver = AsyncMock()
            MockDriver.return_value = mock_driver

            session = await manager.get_or_create("session-1")

            assert session.session_id == "session-1"
            mock_driver.launch.assert_called_once()
            assert "session-1" in manager._sessions

    @pytest.mark.asyncio
    async def test_get_or_create_returns_existing_session(self):
        """Should return existing session if it exists."""
        manager = BrowserSessionManager()

        with patch("pocketclaw.browser.session.BrowserDriver") as MockDriver:
            mock_driver = AsyncMock()
            mock_driver.is_launched = True
            MockDriver.return_value = mock_driver

            session1 = await manager.get_or_create("session-1")
            session2 = await manager.get_or_create("session-1")

            assert session1 is session2
            # Should only launch once
            assert MockDriver.return_value.launch.call_count == 1

    @pytest.mark.asyncio
    async def test_get_or_create_recreates_closed_session(self):
        """Should recreate session if the previous one was closed."""
        manager = BrowserSessionManager()

        with patch("pocketclaw.browser.session.BrowserDriver") as MockDriver:
            mock_driver1 = AsyncMock()
            mock_driver1.is_launched = True
            mock_driver2 = AsyncMock()
            mock_driver2.is_launched = True

            MockDriver.side_effect = [mock_driver1, mock_driver2]

            session1 = await manager.get_or_create("session-1")
            # Simulate driver closed
            mock_driver1.is_launched = False

            session2 = await manager.get_or_create("session-1")

            assert session1 is not session2
            assert mock_driver1.close.call_count == 1

    @pytest.mark.asyncio
    async def test_close_session(self):
        """Should close and remove session."""
        manager = BrowserSessionManager()

        with patch("pocketclaw.browser.session.BrowserDriver") as MockDriver:
            mock_driver = AsyncMock()
            MockDriver.return_value = mock_driver

            await manager.get_or_create("session-1")
            await manager.close_session("session-1")

            assert "session-1" not in manager._sessions
            mock_driver.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_nonexistent_session(self):
        """Should handle closing nonexistent session gracefully."""
        manager = BrowserSessionManager()

        # Should not raise
        await manager.close_session("nonexistent")

    @pytest.mark.asyncio
    async def test_cleanup_idle_sessions(self):
        """Should close sessions idle longer than timeout."""
        manager = BrowserSessionManager()

        with patch("pocketclaw.browser.session.BrowserDriver") as MockDriver:
            mock_driver = AsyncMock()
            mock_driver.is_launched = True
            MockDriver.return_value = mock_driver

            session = await manager.get_or_create("session-1")

            # Manually make session appear old
            from datetime import datetime, timedelta, timezone

            session._last_used_at = datetime.now(tz=timezone.utc) - timedelta(seconds=400)

            # Cleanup with 300 second timeout
            await manager.cleanup_idle(timeout_seconds=300)

            assert "session-1" not in manager._sessions
            mock_driver.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_idle_keeps_active_sessions(self):
        """Should keep recently used sessions."""
        manager = BrowserSessionManager()

        with patch("pocketclaw.browser.session.BrowserDriver") as MockDriver:
            mock_driver = AsyncMock()
            mock_driver.is_launched = True
            MockDriver.return_value = mock_driver

            await manager.get_or_create("session-1")

            # Cleanup with 300 second timeout - session just created
            await manager.cleanup_idle(timeout_seconds=300)

            assert "session-1" in manager._sessions

    @pytest.mark.asyncio
    async def test_close_all(self):
        """Should close all sessions."""
        manager = BrowserSessionManager()

        with patch("pocketclaw.browser.session.BrowserDriver") as MockDriver:
            mock_driver1 = AsyncMock()
            mock_driver2 = AsyncMock()
            MockDriver.side_effect = [mock_driver1, mock_driver2]

            await manager.get_or_create("session-1")
            await manager.get_or_create("session-2")

            await manager.close_all()

            assert len(manager._sessions) == 0
            mock_driver1.close.assert_called_once()
            mock_driver2.close.assert_called_once()

    def test_list_sessions(self):
        """Should list all active session IDs."""
        manager = BrowserSessionManager()

        mock_session1 = MagicMock()
        mock_session1.session_id = "session-1"
        mock_session2 = MagicMock()
        mock_session2.session_id = "session-2"

        manager._sessions["session-1"] = mock_session1
        manager._sessions["session-2"] = mock_session2

        ids = manager.list_sessions()

        assert set(ids) == {"session-1", "session-2"}

    def test_has_session(self):
        """Should check if session exists."""
        manager = BrowserSessionManager()
        manager._sessions["exists"] = MagicMock()

        assert manager.has_session("exists") is True
        assert manager.has_session("nope") is False


class TestGetBrowserSessionManager:
    """Tests for singleton session manager."""

    def test_returns_same_instance(self):
        """Should return the same manager instance (singleton)."""
        manager1 = get_browser_session_manager()
        manager2 = get_browser_session_manager()

        assert manager1 is manager2

    def test_returns_manager_instance(self):
        """Should return a BrowserSessionManager."""
        manager = get_browser_session_manager()
        assert isinstance(manager, BrowserSessionManager)


class TestBrowserSessionManagerConcurrency:
    """Tests for concurrent session operations."""

    @pytest.mark.asyncio
    async def test_concurrent_get_or_create(self):
        """Should handle concurrent session creation safely."""
        manager = BrowserSessionManager()

        with patch("pocketclaw.browser.session.BrowserDriver") as MockDriver:
            mock_driver = AsyncMock()
            mock_driver.is_launched = True
            MockDriver.return_value = mock_driver

            # Concurrent requests for same session
            tasks = [manager.get_or_create("shared-session") for _ in range(5)]
            sessions = await asyncio.gather(*tasks)

            # All should return the same session
            assert all(s is sessions[0] for s in sessions)
            # Should only create one session
            assert MockDriver.return_value.launch.call_count == 1
