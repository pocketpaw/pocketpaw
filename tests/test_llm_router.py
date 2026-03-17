"""Tests for LLMRouter empty response handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.llm.router import _FALLBACK, LLMRouter


@pytest.fixture
def router():
    settings = MagicMock()
    settings.openai_api_key = "test"
    settings.openai_model = "gpt-4"
    settings.anthropic_api_key = "test"
    settings.anthropic_model = "claude-sonnet-4-20250514"
    return LLMRouter(settings)


@pytest.mark.asyncio
async def test_openai_empty_choices(router):
    mock_response = MagicMock()
    mock_response.choices = []
    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create = AsyncMock(return_value=mock_response)
        result = await router._chat_openai("hello")
    assert result == _FALLBACK


@pytest.mark.asyncio
async def test_openai_none_content(router):
    mock_choice = MagicMock()
    mock_choice.message.content = None
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    with patch("openai.AsyncOpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create = AsyncMock(return_value=mock_response)
        result = await router._chat_openai("hello")
    assert result == _FALLBACK


@pytest.mark.asyncio
async def test_anthropic_empty_content(router):
    mock_response = MagicMock()
    mock_response.content = []
    with patch("pocketpaw.llm.client.resolve_llm_client") as mock_resolve:
        mock_client = MagicMock()
        mock_client.create_anthropic_client.return_value.messages.create = AsyncMock(
            return_value=mock_response
        )
        mock_resolve.return_value = mock_client
        result = await router._chat_anthropic("hello")
    assert result == _FALLBACK
