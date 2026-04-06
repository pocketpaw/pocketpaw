# Tests for paw module SoulBootstrapProvider and SoulBridge.
# Created: 2026-03-02
# Covers: get_context() returns BootstrapContext, SoulBridge.observe() and recall()
#         behaviour, and error-swallowing guarantees.

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.bootstrap.protocol import BootstrapContext
from pocketpaw.paw.soul_bridge import SoulBootstrapProvider, SoulBridge

# ---------------------------------------------------------------------------
# Shared soul fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_soul():
    soul = MagicMock()
    soul.name = "TestSoul"
    soul.to_system_prompt.return_value = "I am TestSoul."
    soul.state = MagicMock(mood="curious", energy=85, social_battery=90, tired_threshold=0.3)
    soul.self_model = None
    soul.remember = AsyncMock(return_value="mem_123")
    soul.recall = AsyncMock(
        return_value=[MagicMock(content="fact about project", importance=7, emotion=None)]
    )
    soul.observe = AsyncMock()
    soul.edit_core_memory = AsyncMock()
    soul.save = AsyncMock()
    return soul


# ---------------------------------------------------------------------------
# SoulBootstrapProvider
# ---------------------------------------------------------------------------


class TestSoulBootstrapProvider:
    @pytest.mark.asyncio
    async def test_get_context_returns_bootstrap_context(self, mock_soul):
        provider = SoulBootstrapProvider(mock_soul)

        ctx = await provider.get_context()

        assert isinstance(ctx, BootstrapContext)

    @pytest.mark.asyncio
    async def test_get_context_uses_soul_name(self, mock_soul):
        provider = SoulBootstrapProvider(mock_soul)

        ctx = await provider.get_context()

        assert ctx.name == "TestSoul"

    @pytest.mark.asyncio
    async def test_get_context_uses_system_prompt_as_identity(self, mock_soul):
        provider = SoulBootstrapProvider(mock_soul)

        ctx = await provider.get_context()

        assert ctx.identity == "I am TestSoul."

    @pytest.mark.asyncio
    async def test_get_context_includes_mood_in_style(self, mock_soul):
        provider = SoulBootstrapProvider(mock_soul)

        ctx = await provider.get_context()

        assert "curious" in ctx.style

    @pytest.mark.asyncio
    async def test_get_context_includes_energy_in_style(self, mock_soul):
        provider = SoulBootstrapProvider(mock_soul)

        ctx = await provider.get_context()

        assert "85" in ctx.style

    @pytest.mark.asyncio
    async def test_get_context_default_style_when_no_state_attrs(self):
        soul = MagicMock()
        soul.name = "Bare"
        soul.to_system_prompt.return_value = "Bare soul."
        soul.state = MagicMock(spec=[])  # no mood/energy attrs
        soul.self_model = None
        provider = SoulBootstrapProvider(soul)

        ctx = await provider.get_context()

        assert ctx.style != ""  # falls back to default

    @pytest.mark.asyncio
    async def test_get_context_includes_self_image_domains(self, mock_soul):
        img = MagicMock(domain="Python", confidence=0.9)
        self_model = MagicMock()
        self_model.get_active_self_images.return_value = [img]
        mock_soul.self_model = self_model
        provider = SoulBootstrapProvider(mock_soul)

        ctx = await provider.get_context()

        assert any("Python" in k for k in ctx.knowledge)

    @pytest.mark.asyncio
    async def test_get_context_survives_self_model_exception(self, mock_soul):
        self_model = MagicMock()
        self_model.get_active_self_images.side_effect = RuntimeError("model error")
        mock_soul.self_model = self_model
        provider = SoulBootstrapProvider(mock_soul)

        # Should not raise
        ctx = await provider.get_context()

        assert isinstance(ctx, BootstrapContext)
        # Self-model images should be empty (exception), but memory_count may be present
        assert not any("confidence=" in k for k in ctx.knowledge)

    @pytest.mark.asyncio
    async def test_get_context_soul_string_present(self, mock_soul):
        provider = SoulBootstrapProvider(mock_soul)

        ctx = await provider.get_context()

        assert "soul-protocol" in ctx.soul

    @pytest.mark.asyncio
    async def test_get_context_to_system_prompt_runs_without_error(self, mock_soul):
        provider = SoulBootstrapProvider(mock_soul)

        ctx = await provider.get_context()
        prompt = ctx.to_system_prompt()

        assert "TestSoul" in prompt


# ---------------------------------------------------------------------------
# SoulBridge
# ---------------------------------------------------------------------------


class TestSoulBridgeObserve:
    @pytest.mark.asyncio
    async def test_observe_calls_soul_observe(self, mock_soul):
        """SoulBridge.observe() should call soul.observe() with an Interaction."""
        bridge = SoulBridge(mock_soul)

        # Patch Interaction so soul_protocol need not be installed
        mock_interaction_cls = MagicMock()
        mock_interaction_instance = MagicMock()
        mock_interaction_cls.return_value = mock_interaction_instance

        with patch("pocketpaw.paw.soul_bridge.Interaction", mock_interaction_cls, create=True):
            with patch.dict(
                "sys.modules", {"soul_protocol": MagicMock(Interaction=mock_interaction_cls)}
            ):
                await bridge.observe("Hello", "Hi there")

        mock_soul.observe.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_observe_swallows_import_error(self, mock_soul):
        """If soul_protocol is not installed, observe() must not raise."""
        bridge = SoulBridge(mock_soul)

        with patch.dict("sys.modules", {"soul_protocol": None}):
            # Should complete silently even without soul_protocol
            await bridge.observe("user says hi", "agent says hi back")

    @pytest.mark.asyncio
    async def test_observe_swallows_soul_exception(self, mock_soul):
        """Any exception from soul.observe() must not propagate."""
        mock_soul.observe = AsyncMock(side_effect=RuntimeError("disk error"))
        bridge = SoulBridge(mock_soul)

        mock_interaction_cls = MagicMock()
        with patch.dict(
            "sys.modules", {"soul_protocol": MagicMock(Interaction=mock_interaction_cls)}
        ):
            # Should complete silently
            await bridge.observe("input", "output")


class TestSoulBridgeRecall:
    @pytest.mark.asyncio
    async def test_recall_returns_content_strings(self, mock_soul):
        bridge = SoulBridge(mock_soul)

        results = await bridge.recall("project language")

        assert results == ["fact about project"]

    @pytest.mark.asyncio
    async def test_recall_uses_limit_param(self, mock_soul):
        bridge = SoulBridge(mock_soul)

        await bridge.recall("query", limit=3)

        mock_soul.recall.assert_awaited_once_with("query", limit=3)

    @pytest.mark.asyncio
    async def test_recall_default_limit_is_5(self, mock_soul):
        bridge = SoulBridge(mock_soul)

        await bridge.recall("query")

        mock_soul.recall.assert_awaited_once_with("query", limit=5)

    @pytest.mark.asyncio
    async def test_recall_returns_empty_list_on_exception(self, mock_soul):
        mock_soul.recall = AsyncMock(side_effect=RuntimeError("timeout"))
        bridge = SoulBridge(mock_soul)

        results = await bridge.recall("anything")

        assert results == []

    @pytest.mark.asyncio
    async def test_recall_returns_empty_list_when_soul_recall_returns_empty(self, mock_soul):
        mock_soul.recall = AsyncMock(return_value=[])
        bridge = SoulBridge(mock_soul)

        results = await bridge.recall("anything")

        assert results == []

    @pytest.mark.asyncio
    async def test_recall_extracts_content_attribute_from_memories(self, mock_soul):
        memories = [
            MagicMock(content="first fact"),
            MagicMock(content="second fact"),
        ]
        mock_soul.recall = AsyncMock(return_value=memories)
        bridge = SoulBridge(mock_soul)

        results = await bridge.recall("test")

        assert results == ["first fact", "second fact"]
