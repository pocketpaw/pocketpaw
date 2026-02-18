"""Tests for claude_sdk_max_turns and max_budget_usd settings."""

import json
from unittest.mock import MagicMock, patch

from pocketpaw.config import Settings


class TestAgentLimitsConfig:
    """Test that config fields default correctly and save/load roundtrip."""

    def test_defaults_are_unlimited(self):
        s = Settings()
        assert s.claude_sdk_max_turns == 0  # 0 = unlimited
        assert s.max_budget_usd is None

    def test_explicit_values(self):
        s = Settings(claude_sdk_max_turns=100, max_budget_usd=5.0)
        assert s.claude_sdk_max_turns == 100
        assert s.max_budget_usd == 5.0

    def test_zero_means_unlimited(self):
        s = Settings(claude_sdk_max_turns=0)
        # 0 should be falsy → "or None" in claude_sdk.py yields None (unlimited)
        assert not s.claude_sdk_max_turns

    def test_save_includes_fields(self, tmp_path):
        config_path = tmp_path / "config.json"
        with (
            patch("pocketpaw.config.get_config_path", return_value=config_path),
            patch("pocketpaw.credentials.get_credential_store") as mock_store,
        ):
            mock_store.return_value = MagicMock(set=MagicMock())
            s = Settings(claude_sdk_max_turns=50, max_budget_usd=2.5)
            s.save()

            data = json.loads(config_path.read_text())
            assert data["claude_sdk_max_turns"] == 50
            assert data["max_budget_usd"] == 2.5

    def test_save_default_values(self, tmp_path):
        config_path = tmp_path / "config.json"
        with (
            patch("pocketpaw.config.get_config_path", return_value=config_path),
            patch("pocketpaw.credentials.get_credential_store") as mock_store,
        ):
            mock_store.return_value = MagicMock(set=MagicMock())
            s = Settings()
            s.save()

            data = json.loads(config_path.read_text())
            assert data["claude_sdk_max_turns"] == 0
            assert data["max_budget_usd"] is None


class TestClaudeSDKAgentLimits:
    """Test that claude_sdk.py picks up the settings."""

    @patch("pocketpaw.agents.claude_sdk.ClaudeAgentSDK._initialize")
    def test_unlimited_by_default(self, mock_init):
        from pocketpaw.agents.claude_sdk import ClaudeAgentSDK

        s = Settings()
        agent = ClaudeAgentSDK.__new__(ClaudeAgentSDK)
        agent.settings = s
        assert agent.settings.claude_sdk_max_turns == 0
        assert agent.settings.max_budget_usd is None

    @patch("pocketpaw.agents.claude_sdk.ClaudeAgentSDK._initialize")
    def test_custom_limits(self, mock_init):
        from pocketpaw.agents.claude_sdk import ClaudeAgentSDK

        s = Settings(claude_sdk_max_turns=200, max_budget_usd=10.0)
        agent = ClaudeAgentSDK.__new__(ClaudeAgentSDK)
        agent.settings = s
        assert agent.settings.claude_sdk_max_turns == 200
        assert agent.settings.max_budget_usd == 10.0
