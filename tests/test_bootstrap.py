# Tests for Bootstrap System
# Created: 2026-02-02


import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from pocketpaw.bootstrap.context_builder import AgentContextBuilder
from pocketpaw.bootstrap.default_provider import DefaultBootstrapProvider
from pocketpaw.bootstrap.protocol import BootstrapContext
from pocketpaw.bus.events import Channel


@pytest.fixture
def temp_identity_path():
    """Create a temporary directory for identity files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


class TestBootstrapContext:
    """Tests for BootstrapContext."""

    def test_to_system_prompt(self):
        ctx = BootstrapContext(
            name="TestAgent",
            identity="I am a test agent.",
            soul="I exist to test.",
            style="Be terse.",
            knowledge=["Fact 1", "Fact 2"],
        )

        prompt = ctx.to_system_prompt()
        assert "# Identity: TestAgent" in prompt
        assert "I am a test agent." in prompt
        assert "I exist to test." in prompt
        assert "Be terse." in prompt
        assert "# Key Knowledge" in prompt
        assert "- Fact 1" in prompt

    def test_identity_wrapped_in_xml_tags(self):
        """Identity block must be wrapped in <identity>...</identity> XML tags."""
        ctx = BootstrapContext(
            name="TestAgent",
            identity="I am a test agent.",
            soul="Soul text.",
            style="Style text.",
        )
        prompt = ctx.to_system_prompt()
        assert "<identity>" in prompt
        assert "</identity>" in prompt

    def test_identity_appears_after_instructions(self):
        """Instructions (tool docs) must come before the identity block."""
        ctx = BootstrapContext(
            name="TestAgent",
            identity="I am a test agent.",
            soul="Soul text.",
            style="Style text.",
            instructions="Tool docs go here.",
        )
        prompt = ctx.to_system_prompt()
        instructions_pos = prompt.index("Tool docs go here.")
        identity_pos = prompt.index("<identity>")
        assert instructions_pos < identity_pos, (
            "Instructions should appear before the <identity> block"
        )

    def test_user_profile_inside_identity_block(self):
        """USER.md content must be inside the <identity> block."""
        ctx = BootstrapContext(
            name="TestAgent",
            identity="I am a test agent.",
            soul="Soul.",
            style="Style.",
            user_profile="Name: Alice",
        )
        prompt = ctx.to_system_prompt()
        identity_start = prompt.index("<identity>")
        identity_end = prompt.index("</identity>")
        user_profile_pos = prompt.index("Name: Alice")
        assert identity_start < user_profile_pos < identity_end, (
            "user_profile should be inside the <identity> XML block"
        )

    def test_no_instructions_prompt_has_identity_only(self):
        """When there are no instructions, the prompt is just the identity block."""
        ctx = BootstrapContext(
            name="TestAgent",
            identity="I am a test agent.",
            soul="Soul.",
            style="Style.",
        )
        prompt = ctx.to_system_prompt()
        # Should start with <identity> (after stripping leading whitespace)
        assert prompt.strip().startswith("<identity>")


class TestDefaultBootstrapProvider:
    """Tests for DefaultBootstrapProvider."""

    @pytest.mark.asyncio
    async def test_defaults_creation(self, temp_identity_path):
        provider = DefaultBootstrapProvider(base_path=temp_identity_path)

        # Check files created
        assert (temp_identity_path / "IDENTITY.md").exists()
        assert (temp_identity_path / "SOUL.md").exists()
        assert (temp_identity_path / "STYLE.md").exists()

        # Check content loading
        ctx = await provider.get_context()
        assert ctx.name == "PocketPaw"
        assert "You are PocketPaw" in ctx.identity

    @pytest.mark.asyncio
    async def test_custom_content(self, temp_identity_path):
        # Create provider (makes defaults)
        provider = DefaultBootstrapProvider(base_path=temp_identity_path)

        # Modify files
        (temp_identity_path / "IDENTITY.md").write_text("I am CustomAgent")

        # Reload
        ctx = await provider.get_context()
        assert ctx.identity == "I am CustomAgent"

    @pytest.mark.asyncio
    async def test_get_context_uses_cache(self, temp_identity_path):
        """Second call returns cached content without re-reading from disk."""
        from pocketpaw.bootstrap import default_provider as dp

        dp._identity_file_cache.clear()
        provider = DefaultBootstrapProvider(base_path=temp_identity_path)

        ctx1 = await provider.get_context()
        cached_snapshot = dict(dp._identity_file_cache)
        assert len(cached_snapshot) > 0

        ctx2 = await provider.get_context()
        # Cache entries unchanged (same mtime → no re-read)
        assert dp._identity_file_cache == cached_snapshot
        assert ctx1.identity == ctx2.identity

    @pytest.mark.asyncio
    async def test_cache_invalidates_on_file_change(self, temp_identity_path):
        """Cache is invalidated when a file's mtime changes."""
        import os
        import time

        from pocketpaw.bootstrap import default_provider as dp

        dp._identity_file_cache.clear()
        provider = DefaultBootstrapProvider(base_path=temp_identity_path)

        ctx1 = await provider.get_context()
        assert "You are PocketPaw" in ctx1.identity

        identity = temp_identity_path / "IDENTITY.md"
        identity.write_text("Updated identity", encoding="utf-8")
        # Force mtime forward regardless of filesystem resolution
        future = time.time() + 10
        os.utime(identity, (future, future))

        ctx2 = await provider.get_context()
        assert ctx2.identity == "Updated identity"

    @pytest.mark.asyncio
    async def test_cache_handles_missing_file(self, temp_identity_path):
        """Cache returns empty string for missing files."""
        from pocketpaw.bootstrap import default_provider as dp

        dp._identity_file_cache.clear()
        provider = DefaultBootstrapProvider(base_path=temp_identity_path)

        # Remove USER.md
        (temp_identity_path / "USER.md").unlink()

        ctx = await provider.get_context()
        assert ctx.user_profile == ""


class TestAgentContextBuilder:
    """Tests for AgentContextBuilder."""

    @pytest.mark.asyncio
    async def test_build_full_prompt(self):
        # Mock provider
        mock_provider = MagicMock()
        mock_provider.get_context = AsyncMock(
            return_value=BootstrapContext(
                name="Test", identity="Identity", soul="Soul", style="Style"
            )
        )

        # Mock memory
        mock_memory = MagicMock()
        mock_memory.get_context_for_agent = AsyncMock(return_value="Memory Context")

        builder = AgentContextBuilder(
            bootstrap_provider=mock_provider,
            memory_manager=mock_memory,
        )

        prompt = await builder.build_system_prompt(include_memory=True)

        assert "Identity" in prompt
        assert "Memory Context" in prompt
        assert "# Memory Context" in prompt

    @pytest.mark.asyncio
    async def test_build_with_channel_hint(self):
        mock_provider = MagicMock()
        mock_provider.get_context = AsyncMock(
            return_value=BootstrapContext(
                name="Test", identity="Identity", soul="Soul", style="Style"
            )
        )
        mock_memory = MagicMock()
        mock_memory.get_context_for_agent = AsyncMock(return_value="")

        builder = AgentContextBuilder(bootstrap_provider=mock_provider, memory_manager=mock_memory)

        prompt = await builder.build_system_prompt(channel=Channel.WHATSAPP)
        assert "# Response Format" in prompt
        assert "WhatsApp" in prompt

    @pytest.mark.asyncio
    async def test_build_passthrough_channel_no_hint(self):
        mock_provider = MagicMock()
        mock_provider.get_context = AsyncMock(
            return_value=BootstrapContext(
                name="Test", identity="Identity", soul="Soul", style="Style"
            )
        )
        mock_memory = MagicMock()
        mock_memory.get_context_for_agent = AsyncMock(return_value="")

        builder = AgentContextBuilder(bootstrap_provider=mock_provider, memory_manager=mock_memory)

        prompt = await builder.build_system_prompt(channel=Channel.WEBSOCKET)
        assert "# Response Format" not in prompt

    @pytest.mark.asyncio
    async def test_build_no_memory(self):
        # Mock provider
        mock_provider = MagicMock()
        mock_provider.get_context = AsyncMock(
            return_value=BootstrapContext(
                name="Test", identity="Identity", soul="Soul", style="Style"
            )
        )

        builder = AgentContextBuilder(
            bootstrap_provider=mock_provider,
            memory_manager=MagicMock(),
        )

        prompt = await builder.build_system_prompt(include_memory=False)

        assert "Identity" in prompt
        assert "Memory Context" not in prompt
