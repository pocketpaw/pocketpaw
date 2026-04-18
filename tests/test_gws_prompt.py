"""Tests for GWS CLI system prompt injection."""

from pathlib import Path
from unittest.mock import patch

import pytest

from pocketpaw.bootstrap.context_builder import AgentContextBuilder
from pocketpaw.mcp.config import MCPServerConfig


class TestGwsPromptInjection:
    """Test conditional GWS prompt injection in context builder."""

    @pytest.fixture
    def builder(self):
        return AgentContextBuilder()

    def _make_gws_config(self, *, enabled: bool = True) -> MCPServerConfig:
        return MCPServerConfig(
            name="google-workspace",
            transport="stdio",
            command="gws",
            args=["mcp"],
            enabled=enabled,
        )

    async def test_gws_prompt_injected_when_mcp_active(self, builder):
        """GWS guidance should appear when google-workspace MCP is enabled."""
        configs = [self._make_gws_config(enabled=True)]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=configs):
            prompt = await builder.build_system_prompt(include_memory=False)

        assert "Google Workspace CLI" in prompt
        assert "gws" in prompt

    async def test_gws_prompt_not_injected_when_absent(self, builder):
        """GWS guidance should not appear when no google-workspace MCP is configured."""
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]):
            prompt = await builder.build_system_prompt(include_memory=False)

        assert "Google Workspace CLI" not in prompt

    async def test_gws_prompt_not_injected_when_disabled(self, builder):
        """GWS guidance should not appear when google-workspace MCP is disabled."""
        configs = [self._make_gws_config(enabled=False)]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=configs):
            prompt = await builder.build_system_prompt(include_memory=False)

        assert "Google Workspace CLI" not in prompt

    async def test_gws_prompt_not_injected_for_other_servers(self, builder):
        """GWS guidance should not appear for unrelated MCP servers."""
        configs = [MCPServerConfig(name="github", transport="http", enabled=True)]
        with patch("pocketpaw.mcp.config.load_mcp_config", return_value=configs):
            prompt = await builder.build_system_prompt(include_memory=False)

        assert "Google Workspace CLI" not in prompt


class TestGwsMdFile:
    """Test that gws.md exists and has expected content."""

    def test_gws_md_file_exists(self):
        gws_md = Path(__file__).parent.parent / "src" / "pocketpaw" / "bootstrap" / "gws.md"
        assert gws_md.exists(), "gws.md should exist in bootstrap directory"

    def test_gws_md_has_content(self):
        gws_md = Path(__file__).parent.parent / "src" / "pocketpaw" / "bootstrap" / "gws.md"
        content = gws_md.read_text(encoding="utf-8")
        assert len(content) > 100
        assert "Google Workspace CLI" in content
        assert "--dry-run" in content
        assert "gws auth login" in content
