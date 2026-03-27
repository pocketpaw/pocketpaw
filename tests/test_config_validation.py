"""Tests for the PocketPaw startup configuration validation."""

import pytest
from pydantic import ValidationError
from pocketpaw.config import Settings


def test_config_validation_claude_missing_key():
    """Verify that Claude SDK raises ValidationError if Anthropic key is missing."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            agent_backend="claude_agent_sdk",
            claude_sdk_provider="anthropic",
            anthropic_api_key=None,
            claude_code_oauth_token=None
        )
    assert "requires either anthropic_api_key" in str(excinfo.value)


def test_config_validation_openai_missing_key():
    """Verify that OpenAI backend requires an API key."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            agent_backend="openai_agents",
            openai_agents_provider="openai",
            openai_api_key=""
        )
    assert "requires openai_api_key" in str(excinfo.value)


def test_config_validation_google_missing_key():
    """Verify that Google ADK requires an API key."""
    with pytest.raises(ValidationError) as excinfo:
        Settings(
            agent_backend="google_adk",
            google_adk_provider="google",
            google_api_key=None
        )
    assert "requires google_api_key" in str(excinfo.value)


def test_config_validation_success():
    """Verify that validation passes when keys are provided."""
    settings = Settings(
        agent_backend="openai_agents",
        openai_agents_provider="openai",
        openai_api_key="sk-test-123"
    )
    assert settings.openai_api_key == "sk-test-123"


def test_config_validation_ollama_no_key_needed():
    """Verify that Ollama provider doesn't require an Anthropic key."""
    settings = Settings(
        agent_backend="claude_agent_sdk",
        claude_sdk_provider="ollama",
        anthropic_api_key=None
    )
    assert settings.agent_backend == "claude_agent_sdk"
