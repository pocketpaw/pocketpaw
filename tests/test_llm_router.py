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


# ---------------------------------------------------------------------------
# Provider detection (issue #795)
# ---------------------------------------------------------------------------


class TestDetectBackendProviders:
    """_detect_backend must recognise all supported providers."""

    async def test_detect_openai_compatible(self):
        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:8000/v1",
            openai_compatible_api_key="sk-test",
        )
        router = LLMRouter(settings)
        assert await router._detect_backend() == "openai_compatible"

    async def test_detect_openai_compatible_missing_url(self):
        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="",
        )
        router = LLMRouter(settings)
        assert await router._detect_backend() is None

    async def test_detect_openrouter(self):
        settings = Settings(
            llm_provider="openrouter",
            openrouter_api_key="sk-or-v1-test",
        )
        router = LLMRouter(settings)
        assert await router._detect_backend() == "openrouter"

    async def test_detect_openrouter_fallback_to_compat_key(self):
        settings = Settings(
            llm_provider="openrouter",
            openrouter_api_key=None,
            openai_compatible_api_key="sk-compat",
        )
        router = LLMRouter(settings)
        assert await router._detect_backend() == "openrouter"

    async def test_detect_openrouter_missing_keys(self):
        settings = Settings(
            llm_provider="openrouter",
            openrouter_api_key=None,
            openai_compatible_api_key=None,
        )
        router = LLMRouter(settings)
        assert await router._detect_backend() is None

    async def test_detect_gemini(self):
        settings = Settings(
            llm_provider="gemini",
            google_api_key="AIza-test",
        )
        router = LLMRouter(settings)
        assert await router._detect_backend() == "gemini"

    async def test_detect_gemini_missing_key(self):
        settings = Settings(
            llm_provider="gemini",
            google_api_key=None,
        )
        router = LLMRouter(settings)
        assert await router._detect_backend() is None

    async def test_detect_litellm(self):
        settings = Settings(llm_provider="litellm")
        router = LLMRouter(settings)
        assert await router._detect_backend() == "litellm"

    async def test_auto_selects_gemini_over_ollama(self):
        """Auto mode should prefer Gemini (cloud) over Ollama when key is set."""
        settings = Settings(
            llm_provider="auto",
            anthropic_api_key=None,
            openai_api_key=None,
            google_api_key="AIza-test",
        )
        router = LLMRouter(settings)
        assert await router._detect_backend() == "gemini"

    async def test_auto_selects_openrouter_when_key_set(self):
        settings = Settings(
            llm_provider="auto",
            anthropic_api_key=None,
            openai_api_key=None,
            google_api_key=None,
            openrouter_api_key="sk-or-v1-test",
        )
        router = LLMRouter(settings)
        assert await router._detect_backend() == "openrouter"

    async def test_auto_selects_litellm_when_key_set(self):
        settings = Settings(
            llm_provider="auto",
            anthropic_api_key=None,
            openai_api_key=None,
            google_api_key=None,
            openrouter_api_key=None,
            openai_compatible_base_url="",
            litellm_api_key="sk-litellm-test",
        )
        router = LLMRouter(settings)
        assert await router._detect_backend() == "litellm"


# ---------------------------------------------------------------------------
# chat() routing for OpenAI-compatible providers (issue #795)
# ---------------------------------------------------------------------------


class TestChatOpenAICompatProviders:
    """chat() must route openai_compatible / openrouter / gemini / litellm
    through _chat_openai_compat and return the model response."""

    @pytest.mark.parametrize(
        "provider",
        ["openai_compatible", "openrouter", "gemini", "litellm"],
    )
    async def test_chat_routes_to_openai_compat(self, provider):
        settings = Settings(llm_provider=provider)
        router = LLMRouter(settings)
        router._available_backend = provider

        mock_message = MagicMock()
        mock_message.content = "Hello from provider!"
        mock_choice = MagicMock()
        mock_choice.message = mock_message
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        mock_llm = MagicMock()
        mock_llm.model = "test-model"
        mock_llm.create_openai_client.return_value = mock_client

        with patch("pocketpaw.llm.client.resolve_llm_client", return_value=mock_llm):
            result = await router.chat("hi")

        assert result == "Hello from provider!"
        mock_llm.create_openai_client.assert_called_once()

    @pytest.mark.parametrize(
        "provider",
        ["openai_compatible", "openrouter", "gemini", "litellm"],
    )
    async def test_chat_openai_compat_empty_response(self, provider):
        settings = Settings(llm_provider=provider)
        router = LLMRouter(settings)
        router._available_backend = provider

        mock_response = MagicMock()
        mock_response.choices = []

        mock_client = AsyncMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

        mock_llm = MagicMock()
        mock_llm.model = "test-model"
        mock_llm.create_openai_client.return_value = mock_client

        with patch("pocketpaw.llm.client.resolve_llm_client", return_value=mock_llm):
            result = await router.chat("hi")

        assert result == FALLBACK
