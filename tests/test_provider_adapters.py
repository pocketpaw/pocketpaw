"""Tests for LLM provider adapters."""

import sys
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.llm.providers import ProviderConfig, get_adapter, resolve_model
from pocketpaw.llm.providers.base import PROVIDER_DEFAULT_MODELS


def _mock_settings(**overrides):
    """Create a mock Settings with sensible defaults."""
    defaults = {
        "anthropic_api_key": "sk-ant-test",
        "anthropic_model": "claude-sonnet-4-6",
        "ollama_host": "http://localhost:11434",
        "ollama_model": "llama3.2",
        "openai_api_key": "sk-test",
        "openai_model": "gpt-5.2",
        "openai_compatible_base_url": "http://local:8080/v1",
        "openai_compatible_api_key": "oai-compat-key",
        "openai_compatible_model": "my-model",
        "openrouter_api_key": "sk-or-v1-test",
        "openrouter_model": "anthropic/claude-sonnet-4-6",
        "google_api_key": "google-test-key",
        "gemini_model": "gemini-3-pro-preview",
        "litellm_api_base": "http://localhost:4000",
        "litellm_api_key": "lit-key",
        "litellm_model": "anthropic/claude-sonnet-4-6",
        "claude_sdk_model": "",
        "openai_agents_model": "",
        "google_adk_model": "",
        "copilot_sdk_model": "",
        "codex_cli_model": "gpt-5.3-codex",
    }
    defaults.update(overrides)
    return MagicMock(**defaults)


# -- Registry --


class TestRegistry:
    def test_known_providers(self):
        for name in ("anthropic", "ollama", "openai_compatible", "openrouter", "gemini", "litellm"):
            adapter = get_adapter(name)
            assert adapter.name == name

    def test_unknown_raises(self):
        with pytest.raises(KeyError, match="Unknown provider 'banana'"):
            get_adapter("banana")


# -- resolve_model --


class TestResolveModel:
    def test_backend_specific_wins(self):
        s = _mock_settings(claude_sdk_model="claude-opus-5", anthropic_model="claude-sonnet-4-6")
        assert resolve_model(s, "claude_agent_sdk", "anthropic") == "claude-opus-5"

    def test_provider_fallback(self):
        s = _mock_settings(claude_sdk_model="", anthropic_model="claude-haiku-4-5")
        assert resolve_model(s, "claude_agent_sdk", "anthropic") == "claude-haiku-4-5"

    def test_default_fallback(self):
        s = _mock_settings(claude_sdk_model="", anthropic_model="")
        result = resolve_model(s, "claude_agent_sdk", "anthropic")
        assert result == PROVIDER_DEFAULT_MODELS["anthropic"]

    def test_litellm_model(self):
        s = _mock_settings(openai_agents_model="", litellm_model="huggingface/llama-70b")
        assert resolve_model(s, "openai_agents", "litellm") == "huggingface/llama-70b"


# -- AnthropicAdapter --


class TestAnthropicAdapter:
    def test_resolve_config(self):
        adapter = get_adapter("anthropic")
        s = _mock_settings(claude_sdk_model="claude-opus-5")
        config = adapter.resolve_config(s, "claude_agent_sdk")
        assert config.provider == "anthropic"
        assert config.model == "claude-opus-5"
        assert config.api_key == "sk-ant-test"
        assert config.base_url is None

    def test_build_env_dict(self):
        adapter = get_adapter("anthropic")
        config = ProviderConfig(provider="anthropic", model="x", api_key="sk-ant-123")
        env = adapter.build_env_dict(config)
        assert env == {"ANTHROPIC_API_KEY": "sk-ant-123"}

    def test_build_env_dict_no_key(self):
        adapter = get_adapter("anthropic")
        config = ProviderConfig(provider="anthropic", model="x")
        assert adapter.build_env_dict(config) == {}

    def test_format_error_auth(self):
        adapter = get_adapter("anthropic")
        config = ProviderConfig(provider="anthropic", model="x")
        msg = adapter.format_error(config, Exception("Invalid API key"))
        assert "API key" in msg


# -- OllamaAdapter --


class TestOllamaAdapter:
    def test_resolve_config_no_api_key(self):
        adapter = get_adapter("ollama")
        config = adapter.resolve_config(_mock_settings(), "claude_agent_sdk")
        assert config.api_key is None
        assert config.base_url == "http://localhost:11434"

    def test_build_env_dict(self):
        adapter = get_adapter("ollama")
        config = ProviderConfig(provider="ollama", model="x", base_url="http://my-ollama:11434")
        env = adapter.build_env_dict(config)
        assert env["ANTHROPIC_BASE_URL"] == "http://my-ollama:11434"
        assert env["ANTHROPIC_API_KEY"] == "ollama"

    def test_build_openai_client_appends_v1(self):
        adapter = get_adapter("ollama")
        config = ProviderConfig(provider="ollama", model="x", base_url="http://localhost:11434")
        with patch("openai.AsyncOpenAI") as mock_cls:
            adapter.build_openai_client(config)
            mock_cls.assert_called_once()
            assert mock_cls.call_args.kwargs["base_url"] == "http://localhost:11434/v1"

    def test_format_error_not_found(self):
        adapter = get_adapter("ollama")
        config = ProviderConfig(provider="ollama", model="phi4")
        msg = adapter.format_error(config, Exception("model not found"))
        assert "phi4" in msg
        assert "not found" in msg

    def test_format_error_connection(self):
        adapter = get_adapter("ollama")
        config = ProviderConfig(provider="ollama", model="x", base_url="http://localhost:11434")
        msg = adapter.format_error(config, Exception("connection refused"))
        assert "Cannot connect" in msg


# -- OpenRouterAdapter --


class TestOpenRouterAdapter:
    def test_build_env_dict_uses_auth_token(self):
        adapter = get_adapter("openrouter")
        config = ProviderConfig(
            provider="openrouter",
            model="x",
            api_key="sk-or-key",
            base_url="https://openrouter.ai/api/v1",
        )
        env = adapter.build_env_dict(config)
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or-key"
        assert env["ANTHROPIC_API_KEY"] == ""

    def test_build_env_dict_strips_v1(self):
        adapter = get_adapter("openrouter")
        config = ProviderConfig(
            provider="openrouter",
            model="x",
            base_url="https://openrouter.ai/api/v1",
        )
        env = adapter.build_env_dict(config)
        assert env["ANTHROPIC_BASE_URL"] == "https://openrouter.ai/api"


# -- GeminiAdapter --


class TestGeminiAdapter:
    def test_resolve_config(self):
        adapter = get_adapter("gemini")
        config = adapter.resolve_config(_mock_settings(), "google_adk")
        assert config.api_key == "google-test-key"
        assert "generativelanguage" in config.base_url

    def test_format_error_auth(self):
        adapter = get_adapter("gemini")
        config = ProviderConfig(provider="gemini", model="x")
        msg = adapter.format_error(config, Exception("401 unauthorized"))
        assert "API key" in msg


# -- LiteLLMAdapter --


class TestLiteLLMAdapter:
    def test_resolve_config(self):
        adapter = get_adapter("litellm")
        config = adapter.resolve_config(_mock_settings(), "openai_agents")
        assert config.provider == "litellm"
        assert config.api_key == "lit-key"
        assert config.base_url == "http://localhost:4000"

    def test_build_env_dict(self):
        adapter = get_adapter("litellm")
        config = ProviderConfig(
            provider="litellm", model="x", api_key="key", base_url="http://proxy:4000"
        )
        env = adapter.build_env_dict(config)
        assert env["ANTHROPIC_BASE_URL"] == "http://proxy:4000"
        assert env["ANTHROPIC_API_KEY"] == "key"

    def test_build_agents_model_native(self):
        adapter = get_adapter("litellm")
        config = ProviderConfig(provider="litellm", model="gpt-4o", api_key="k")
        mock_model = MagicMock()
        with patch(
            "pocketpaw.llm.providers.litellm.LitellmModel",
            create=True,
        ) as mock_cls:
            mock_cls.return_value = mock_model
            # Patch the import inside the method
            import sys

            fake_module = MagicMock()
            fake_module.LitellmModel = mock_cls
            sys.modules["agents.extensions.models.litellm_model"] = fake_module
            try:
                result = adapter.build_agents_model(config)
                assert result is mock_model
            finally:
                del sys.modules["agents.extensions.models.litellm_model"]

    def test_build_agents_model_proxy_mode(self):
        """When base_url is set, use OpenAI-compat client (proxy handles routing)."""
        adapter = get_adapter("litellm")
        config = ProviderConfig(
            provider="litellm",
            model="gpt-4o",
            api_key="k",
            base_url="http://proxy:4000",
        )
        mock_oai_model = MagicMock()
        with (
            patch.dict(
                sys.modules,
                {
                    "agents.models.openai_chatcompletions": MagicMock(
                        OpenAIChatCompletionsModel=MagicMock(return_value=mock_oai_model)
                    ),
                },
            ),
            patch("openai.AsyncOpenAI"),
        ):
            result = adapter.build_agents_model(config)
            assert result is mock_oai_model

    def test_build_adk_model_native(self):
        adapter = get_adapter("litellm")
        config = ProviderConfig(provider="litellm", model="gpt-4o")
        import sys

        fake_module = MagicMock()
        fake_litellm_cls = MagicMock()
        fake_module.LiteLlm = fake_litellm_cls
        sys.modules["google.adk.models.lite_llm"] = fake_module
        try:
            adapter.build_adk_model(config)
            fake_litellm_cls.assert_called_once_with(model="gpt-4o")
        finally:
            del sys.modules["google.adk.models.lite_llm"]

    def test_format_error_connection(self):
        adapter = get_adapter("litellm")
        config = ProviderConfig(provider="litellm", model="x", base_url="http://proxy:4000")
        msg = adapter.format_error(config, Exception("connection refused"))
        assert "Cannot connect" in msg
        assert "proxy:4000" in msg


# -- LLMClient delegation --


class TestLLMClientDelegation:
    """Verify LLMClient delegates to adapters correctly."""

    def test_to_sdk_env_anthropic(self):
        from pocketpaw.llm.client import LLMClient

        client = LLMClient(
            provider="anthropic",
            model="claude-sonnet-4-6",
            api_key="sk-ant-test",
            ollama_host="http://localhost:11434",
        )
        env = client.to_sdk_env()
        assert env == {"ANTHROPIC_API_KEY": "sk-ant-test"}

    def test_to_sdk_env_ollama(self):
        from pocketpaw.llm.client import LLMClient

        client = LLMClient(
            provider="ollama",
            model="llama3",
            api_key=None,
            ollama_host="http://localhost:11434",
        )
        env = client.to_sdk_env()
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
        assert env["ANTHROPIC_API_KEY"] == "ollama"

    def test_to_sdk_env_openrouter(self):
        from pocketpaw.llm.client import LLMClient

        client = LLMClient(
            provider="openai_compatible",
            model="test",
            api_key="sk-or-key",
            ollama_host="http://localhost:11434",
            openai_compatible_base_url="https://openrouter.ai/api/v1",
        )
        env = client.to_sdk_env()
        assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-or-key"
        assert env["ANTHROPIC_API_KEY"] == ""

    def test_format_api_error_delegates(self):
        from pocketpaw.llm.client import LLMClient

        client = LLMClient(
            provider="ollama",
            model="phi4",
            api_key=None,
            ollama_host="http://localhost:11434",
        )
        msg = client.format_api_error(Exception("model not found"))
        assert "phi4" in msg

    def test_resolve_llm_client_unknown_falls_back(self):
        from pocketpaw.llm.client import resolve_llm_client

        s = _mock_settings(llm_provider="nonexistent_provider")
        client = resolve_llm_client(s, force_provider="nonexistent_provider")
        assert client.provider == "anthropic"
