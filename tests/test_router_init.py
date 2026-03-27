"""Tests for AgentRouter initialization and edge cases."""

from unittest.mock import MagicMock, patch
import pytest
from pocketpaw.agents.router import AgentRouter


class MockSettings:
    """Minimal settings mock."""
    def __init__(self, backend="claude_agent_sdk", fallbacks=None):
        self.agent_backend = backend
        self.fallback_backends = fallbacks or []


def test_router_init_invalid_backend(caplog):
    """Verify that router falls back to 'claude_agent_sdk' if configured backend is missing."""
    settings = MockSettings(backend="invalid_unknown_backend")
    
    # Mock registry to return None for the invalid backend, but a real class for the fallback
    def mock_get_backend(name):
        if name == "claude_agent_sdk":
            mock_cls = MagicMock()
            mock_cls.info.return_value = MagicMock(display_name="Claude")
            return mock_cls
        return None

    with patch("pocketpaw.agents.router.get_backend_class", side_effect=mock_get_backend):
        router = AgentRouter(settings)
        # Should have defaulted to claude_agent_sdk
        assert router._active_backend_name == "claude_agent_sdk"
        assert "unavailable — falling back to claude_agent_sdk" in caplog.text


def test_router_init_exception_handling(caplog):
    """Verify that router handles exceptions during backend instantiation gracefully."""
    settings = MockSettings(backend="broken_backend")
    
    class BrokenBackend:
        def __init__(self, settings):
            raise RuntimeError("Initialization failed!")
        @classmethod
        def info(cls): return MagicMock()

    with patch("pocketpaw.agents.router.get_backend_class", return_value=BrokenBackend):
        router = AgentRouter(settings)
        # Should log error and set active backend to None
        assert router._active_backend_name is None
        assert "Failed to initialize 'broken_backend' backend" in caplog.text


@pytest.mark.asyncio
async def test_router_stop_resilience():
    """Verify that router.stop() handles errors in backend stop methods."""
    settings = MockSettings()
    
    mock_primary = MagicMock()
    mock_primary.stop.side_effect = Exception("Stop failed!")
    
    mock_fallback = MagicMock()
    mock_fallback.stop.side_effect = Exception("Fallback stop failed!")

    with patch("pocketpaw.agents.router.get_backend_class", return_value=lambda s: mock_primary):
        router = AgentRouter(settings)
        router._backend = mock_primary
        router._fallback_instances = {"fallback": mock_fallback}
        
        # This should not raise
        await router.stop()
        
        mock_primary.stop.assert_called_once()
        mock_fallback.stop.assert_called_once()


def test_router_get_backend_info_none():
    """Verify get_backend_info returns None if no backend is initialized."""
    settings = MockSettings()
    with patch("pocketpaw.agents.router.get_backend_class", return_value=None):
        # Force a state where _backend is None
        router = AgentRouter(settings)
        router._backend = None 
        assert router.get_backend_info() is None
