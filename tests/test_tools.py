"""Unit tests for PocketPaw tools."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestStatusTool:
    """Tests for status tool."""

    def test_get_system_status_returns_string(self):
        """Status should return a formatted string."""
        from pocketpaw.tools import status

        result = status.get_system_status()

        assert isinstance(result, str)
        assert "System Status" in result
        assert "CPU" in result
        assert "RAM" in result
        assert "Disk" in result

    def test_get_system_status_contains_percentages(self):
        """Status should contain percentage values."""
        from pocketpaw.tools import status

        result = status.get_system_status()

        # Should have percentage signs
        assert "%" in result


class TestFetchTool:
    """Tests for fetch tool."""

    def test_is_safe_path_within_jail(self, tmp_path):
        """Paths within jail should be safe."""
        from pocketpaw.tools.fetch import is_safe_path

        jail = tmp_path
        safe_path = tmp_path / "subdir"
        safe_path.mkdir()

        assert is_safe_path(safe_path, jail) is True

    def test_is_safe_path_outside_jail(self, tmp_path):
        """Paths outside jail should be unsafe."""
        from pocketpaw.tools.fetch import is_safe_path

        jail = tmp_path / "jail"
        jail.mkdir()
        outside_path = tmp_path / "outside"
        outside_path.mkdir()

        assert is_safe_path(outside_path, jail) is False

    def test_is_safe_path_parent_traversal(self, tmp_path):
        """Parent traversal should be blocked."""
        from pocketpaw.tools.fetch import is_safe_path

        jail = tmp_path / "jail"
        jail.mkdir()
        traversal_path = jail / ".." / "outside"

        assert is_safe_path(traversal_path, jail) is False

    @pytest.mark.asyncio
    async def test_handle_path_directory(self, tmp_path):
        """Should handle directory paths."""
        from pocketpaw.tools.fetch import handle_path

        result = await handle_path(str(tmp_path), tmp_path)

        assert result["type"] == "directory"
        assert "keyboard" in result

    @pytest.mark.asyncio
    async def test_handle_path_file(self, tmp_path):
        """Should handle file paths."""
        from pocketpaw.tools.fetch import handle_path

        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        result = await handle_path(str(test_file), tmp_path)

        assert result["type"] == "file"
        assert result["filename"] == "test.txt"

    @pytest.mark.asyncio
    async def test_handle_path_outside_jail(self, tmp_path):
        """Should reject paths outside jail."""
        from pocketpaw.tools.fetch import handle_path

        jail = tmp_path / "jail"
        jail.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()

        result = await handle_path(str(outside), jail)

        assert result["type"] == "error"


class TestScreenshotTool:
    """Tests for screenshot tool."""

    def test_take_screenshot_returns_bytes_or_none(self):
        """Screenshot should return bytes or None."""
        from pocketpaw.tools import screenshot

        result = screenshot.take_screenshot()

        # Should be bytes or None (depending on display availability)
        assert result is None or isinstance(result, bytes)

    @patch("pocketpaw.tools.screenshot.PYAUTOGUI_AVAILABLE", False)
    def test_take_screenshot_without_pyautogui(self):
        """Should return None when pyautogui unavailable."""
        from pocketpaw.tools import screenshot

        # Force reimport to pick up patched value
        with patch.object(screenshot, "PYAUTOGUI_AVAILABLE", False):
            result = screenshot.take_screenshot()
            assert result is None


class TestConfig:
    """Tests for configuration."""

    def test_settings_defaults(self):
        """Settings should have sensible defaults."""
        from pocketpaw.config import Settings

        settings = Settings()

        assert settings.agent_backend == "claude_agent_sdk"  # New default
        assert settings.llm_provider == "auto"
        assert settings.web_port == 8888
        assert settings.ollama_model == "llama3.2"

    def test_settings_save_and_load(self, tmp_path, monkeypatch):
        """Settings should persist to disk."""
        from pocketpaw.config import Settings, get_config_path
        from pocketpaw.credentials import CredentialStore, get_credential_store

        # Mock config path to use temp directory
        config_file = tmp_path / "config.json"
        monkeypatch.setattr("pocketpaw.config.get_config_path", lambda: config_file)

        # Mock credential store to use temp directory (avoid polluting real secrets)
        test_store = CredentialStore(config_dir=tmp_path)
        monkeypatch.setattr("pocketpaw.credentials.get_credential_store", lambda: test_store)

        # Create and save settings
        settings = Settings(telegram_bot_token="test-token", allowed_user_id=12345)
        settings.save()

        # Verify file exists
        assert config_file.exists()

        # Load and verify
        loaded = Settings.load()
        assert loaded.telegram_bot_token == "test-token"
        assert loaded.allowed_user_id == 12345

    def test_get_config_dir_creates_directory(self, tmp_path, monkeypatch):
        """Config dir should be created if not exists."""
        from pocketpaw.config import get_config_dir

        # Mock home to use temp
        new_home = tmp_path / "home"
        new_home.mkdir()
        monkeypatch.setattr(Path, "home", lambda: new_home)

        result = get_config_dir()

        assert result.exists()
        assert result.name == ".pocketpaw"


class TestLLMRouter:
    """Tests for LLM router."""

    def test_router_initialization(self):
        """Router should initialize without errors."""
        from pocketpaw.config import Settings
        from pocketpaw.llm.router import LLMRouter

        settings = Settings()
        router = LLMRouter(settings)

        assert router.conversation_history == []

    def test_router_clear_history(self):
        """Should clear conversation history."""
        from pocketpaw.config import Settings
        from pocketpaw.llm.router import LLMRouter

        settings = Settings()
        router = LLMRouter(settings)
        router.conversation_history = [{"role": "user", "content": "test"}]

        router.clear_history()

        assert router.conversation_history == []

    @pytest.mark.asyncio
    async def test_router_no_backend_returns_error(self):
        """Should return error when no backend available."""
        from pocketpaw.config import Settings
        from pocketpaw.llm.router import LLMRouter

        settings = Settings(
            llm_provider="openai",
            openai_api_key=None,  # No key
        )
        router = LLMRouter(settings)

        result = await router.chat("Hello")

        assert "No LLM backend available" in result


class TestAgentRouter:
    """Tests for agent router."""

    def test_router_initializes_claude_agent_sdk(self):
        """Should initialize with claude_agent_sdk backend."""
        from pocketpaw.config import Settings
        from pocketpaw.agents.router import AgentRouter

        settings = Settings(agent_backend="claude_agent_sdk", anthropic_api_key="test")
        router = AgentRouter(settings)

        assert router._backend is not None

    def test_router_legacy_backend_falls_back(self):
        """Legacy backend names should fall back to claude_agent_sdk."""
        from pocketpaw.config import Settings
        from pocketpaw.agents.router import AgentRouter

        settings = Settings(agent_backend="open_interpreter", anthropic_api_key="test")
        router = AgentRouter(settings)

        # Should have fallen back to claude_agent_sdk
        assert router._backend is not None
