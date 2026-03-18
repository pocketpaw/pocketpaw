"""Tests for LLMRouter empty-response handling (issue #664).

Verifies that LLMRouter._chat_openai and LLMRouter._chat_anthropic
return a safe fallback string instead of raising IndexError when the
upstream LLM API returns an empty choices/content list.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.config import Settings
from pocketpaw.llm.router import LLMRouter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FALLBACK = "I'm sorry, I received an empty response. Please try again."


def _make_router(provider: str = "openai") -> LLMRouter:
    settings = Settings(
        llm_provider=provider,
        openai_api_key="sk-test",
        anthropic_api_key="sk-ant-test",
    )
    router = LLMRouter(settings)
    return router


# ---------------------------------------------------------------------------
# OpenAI path
# ---------------------------------------------------------------------------


class TestChatOpenAIEmptyResponse:
    async def test_empty_choices_returns_fallback(self):
        """IndexError must NOT be raised; fallback text is returned instead."""
        router = _make_router("openai")
        router._available_backend = "openai"

        mock_response = MagicMock()
        mock_response.choices = []

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        # AsyncOpenAI is imported lazily inside _chat_openai — patch at source
        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await router._chat_openai("hello")

        assert result == FALLBACK

    async def test_empty_choices_does_not_raise(self):
        """Regression: confirm no IndexError propagates to the caller."""
        router = _make_router("openai")
        router._available_backend = "openai"

        mock_response = MagicMock()
        mock_response.choices = []

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            try:
                await router._chat_openai("hello")
            except IndexError:
                pytest.fail("_chat_openai raised IndexError on empty choices")

    async def test_normal_response_still_works(self):
        """Non-empty choices returns the expected content."""
        router = _make_router("openai")
        router._available_backend = "openai"

        mock_message = MagicMock()
        mock_message.content = "Hello, world!"
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await router._chat_openai("hello")

        assert result == "Hello, world!"


# ---------------------------------------------------------------------------
# Anthropic path
# ---------------------------------------------------------------------------


class TestChatAnthropicEmptyResponse:
    async def test_empty_content_returns_fallback(self):
        """IndexError must NOT be raised; fallback text is returned instead."""
        router = _make_router("anthropic")
        router._available_backend = "anthropic"

        mock_response = MagicMock()
        mock_response.content = []

        mock_anthropic_client = AsyncMock()
        mock_anthropic_client.messages.create = AsyncMock(return_value=mock_response)

        mock_llm = MagicMock()
        mock_llm.create_anthropic_client.return_value = mock_anthropic_client

        # resolve_llm_client is imported lazily inside _chat_anthropic — patch at source
        with patch("pocketpaw.llm.client.resolve_llm_client", return_value=mock_llm):
            result = await router._chat_anthropic("hello")

        assert result == FALLBACK

    async def test_empty_content_does_not_raise(self):
        """Regression: confirm no IndexError propagates to the caller."""
        router = _make_router("anthropic")
        router._available_backend = "anthropic"

        mock_response = MagicMock()
        mock_response.content = []

        mock_anthropic_client = AsyncMock()
        mock_anthropic_client.messages.create = AsyncMock(return_value=mock_response)

        mock_llm = MagicMock()
        mock_llm.create_anthropic_client.return_value = mock_anthropic_client

        with patch("pocketpaw.llm.client.resolve_llm_client", return_value=mock_llm):
            try:
                await router._chat_anthropic("hello")
            except IndexError:
                pytest.fail("_chat_anthropic raised IndexError on empty content")

    async def test_normal_response_still_works(self):
        """Non-empty content returns the expected text."""
        router = _make_router("anthropic")
        router._available_backend = "anthropic"

        mock_block = MagicMock()
        mock_block.text = "Hi there!"
        mock_response = MagicMock()
        mock_response.content = [mock_block]

        mock_anthropic_client = AsyncMock()
        mock_anthropic_client.messages.create = AsyncMock(return_value=mock_response)

        mock_llm = MagicMock()
        mock_llm.create_anthropic_client.return_value = mock_anthropic_client

        with patch("pocketpaw.llm.client.resolve_llm_client", return_value=mock_llm):
            result = await router._chat_anthropic("hello")

        assert result == "Hi there!"


# ---------------------------------------------------------------------------
# Integration: chat() surface
# ---------------------------------------------------------------------------


class TestChatFallbackIntegration:
    async def test_chat_openai_empty_response_returns_fallback(self):
        """End-to-end: chat() surfaces the fallback when OpenAI returns empty."""
        router = _make_router("openai")
        router._available_backend = "openai"

        mock_response = MagicMock()
        mock_response.choices = []

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        with patch("openai.AsyncOpenAI", return_value=mock_client):
            result = await router.chat("ping")

        assert result == FALLBACK

    async def test_chat_anthropic_empty_response_returns_fallback(self):
        """End-to-end: chat() surfaces the fallback when Anthropic returns empty."""
        router = _make_router("anthropic")
        router._available_backend = "anthropic"

        mock_response = MagicMock()
        mock_response.content = []

        mock_anthropic_client = AsyncMock()
        mock_anthropic_client.messages.create = AsyncMock(return_value=mock_response)

        mock_llm = MagicMock()
        mock_llm.create_anthropic_client.return_value = mock_anthropic_client

        with patch("pocketpaw.llm.client.resolve_llm_client", return_value=mock_llm):
            result = await router.chat("ping")

        assert result == FALLBACK
