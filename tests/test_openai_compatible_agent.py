"""Tests for OpenAI-compatible endpoint integration."""

from unittest.mock import AsyncMock, MagicMock, patch

from pocketpaw.llm.client import resolve_llm_client

# ---------------------------------------------------------------------------
# LLMClient — OpenAI-compatible provider detection
# ---------------------------------------------------------------------------


class TestLLMClientOpenAICompatible:
    """Verify LLMClient correctly handles OpenAI-compatible provider."""

    def test_provider_detection(self):
        """When llm_provider='openai_compatible', client detects it."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_model="gpt-4o",
        )
        llm = resolve_llm_client(settings)
        assert llm.is_openai_compatible
        assert not llm.is_ollama
        assert not llm.is_anthropic

    def test_model_resolved(self):
        """Model name is set from openai_compatible_model."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_model="my-custom-model",
        )
        llm = resolve_llm_client(settings)
        assert llm.model == "my-custom-model"

    def test_base_url_stored(self):
        """Base URL is carried through to the LLMClient."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://myhost:8080/v1",
            openai_compatible_model="model-x",
        )
        llm = resolve_llm_client(settings)
        assert llm.openai_compatible_base_url == "http://myhost:8080/v1"

    def test_api_key_carried(self):
        """API key flows to the LLMClient."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_api_key="sk-custom-key",
            openai_compatible_model="model-x",
        )
        llm = resolve_llm_client(settings)
        assert llm.api_key == "sk-custom-key"

    def test_api_key_optional(self):
        """API key can be None."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_model="model-x",
        )
        llm = resolve_llm_client(settings)
        assert llm.api_key is None


class TestLLMClientOpenAICompatibleEnv:
    """Verify env var construction for Claude SDK subprocess."""

    def test_env_vars_with_key(self):
        """Env dict includes ANTHROPIC_BASE_URL and ANTHROPIC_API_KEY."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_api_key="sk-custom",
            openai_compatible_model="model-x",
        )
        llm = resolve_llm_client(settings)
        env = llm.to_sdk_env()
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:4000/v1"
        assert env["ANTHROPIC_API_KEY"] == "sk-custom"

    def test_env_vars_without_key(self):
        """Env dict uses 'not-needed' when no API key is set."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_model="model-x",
        )
        llm = resolve_llm_client(settings)
        env = llm.to_sdk_env()
        assert env["ANTHROPIC_BASE_URL"] == "http://localhost:4000/v1"
        assert env["ANTHROPIC_API_KEY"] == "not-needed"


class TestLLMClientOpenAICompatibleClient:
    """Verify Anthropic client creation for OpenAI-compatible provider."""

    @patch("anthropic.AsyncAnthropic")
    def test_creates_client_with_base_url(self, mock_anthropic):
        """create_anthropic_client() uses the custom base URL."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_api_key="sk-test",
            openai_compatible_model="model-x",
        )
        llm = resolve_llm_client(settings)
        llm.create_anthropic_client()

        mock_anthropic.assert_called_once_with(
            base_url="http://localhost:4000/v1",
            api_key="sk-test",
            timeout=120.0,
            max_retries=1,
        )

    @patch("anthropic.AsyncAnthropic")
    def test_creates_client_without_key(self, mock_anthropic):
        """create_anthropic_client() uses 'not-needed' when no key."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_model="model-x",
        )
        llm = resolve_llm_client(settings)
        llm.create_anthropic_client()

        mock_anthropic.assert_called_once_with(
            base_url="http://localhost:4000/v1",
            api_key="not-needed",
            timeout=120.0,
            max_retries=1,
        )


class TestLLMClientOpenAICompatibleErrors:
    """Verify error formatting for OpenAI-compatible provider."""

    def test_connection_error(self):
        """Connection errors show the base URL."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_model="model-x",
        )
        llm = resolve_llm_client(settings)
        msg = llm.format_api_error(ConnectionError("Connection refused"))
        assert "localhost:4000" in msg
        assert "Cannot connect" in msg

    def test_generic_error(self):
        """Generic errors include the base URL in the message."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_model="model-x",
        )
        llm = resolve_llm_client(settings)
        msg = llm.format_api_error(RuntimeError("Something went wrong"))
        assert "OpenAI-compatible" in msg
        assert "localhost:4000" in msg

    def test_model_not_found_via_stderr(self):
        """When stderr contains model error, surfaces model name and hint."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="https://integrate.api.nvidia.com/v1",
            openai_compatible_model="moonshotai/kimi-k2.5",
        )
        llm = resolve_llm_client(settings)
        msg = llm.format_api_error(
            RuntimeError("Command failed with exit code 1"),
            stderr=(
                "There's an issue with the selected model (moonshotai/kimi-k2.5). "
                "It may not exist or you may not have access to it."
            ),
        )
        assert "moonshotai/kimi-k2.5" in msg
        assert "not available" in msg
        assert "nvidia.com" in msg
        # Should NOT say "server is running"
        assert "server" not in msg.lower() or "running" not in msg.lower()

    def test_stderr_surfaced_in_generic_error(self):
        """When stderr has content, it replaces the generic error message."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_model="model-x",
        )
        llm = resolve_llm_client(settings)
        msg = llm.format_api_error(
            RuntimeError("Command failed with exit code 1"),
            stderr="Rate limit exceeded. Try again later.",
        )
        assert "Rate limit exceeded" in msg

    def test_auth_error(self):
        """Authentication errors suggest checking API key."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_model="model-x",
        )
        llm = resolve_llm_client(settings)
        msg = llm.format_api_error(
            RuntimeError("Unauthorized"),
            stderr="Authentication failed: invalid API key",
        )
        assert "Authentication" in msg




# ---------------------------------------------------------------------------
# Claude SDK + OpenAI-compatible (logic tests via LLMClient)
# ---------------------------------------------------------------------------


class TestClaudeSDKOpenAICompatibleLogic:
    """Test OpenAI-compatible provider detection logic using LLMClient."""

    def test_smart_routing_skipped(self):
        """Verify smart routing skip condition for OpenAI-compatible."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_model="model-x",
            smart_routing_enabled=True,
        )
        llm = resolve_llm_client(settings)
        should_route = (
            settings.smart_routing_enabled and not llm.is_ollama and not llm.is_openai_compatible
        )
        assert should_route is False

    def test_smart_routing_enabled_for_anthropic(self):
        """Verify smart routing is NOT skipped for Anthropic."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="anthropic",
            anthropic_api_key="sk-test",
            smart_routing_enabled=True,
        )
        llm = resolve_llm_client(settings)
        should_route = (
            settings.smart_routing_enabled and not llm.is_ollama and not llm.is_openai_compatible
        )
        assert should_route is True




# ---------------------------------------------------------------------------
# check_openai_compatible CLI
# ---------------------------------------------------------------------------


class TestCheckOpenAICompatible:
    """Tests for the --check-openai-compatible CLI command."""

    async def test_empty_base_url_returns_1(self):
        """When base URL is empty, returns exit code 1."""
        from pocketpaw.__main__ import check_openai_compatible
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="",
            openai_compatible_model="model-x",
        )
        exit_code = await check_openai_compatible(settings)
        assert exit_code == 1

    async def test_empty_model_returns_1(self):
        """When model is empty, returns exit code 1."""
        from pocketpaw.__main__ import check_openai_compatible
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_model="",
        )
        exit_code = await check_openai_compatible(settings)
        assert exit_code == 1

    async def test_api_failure_returns_1(self):
        """When the API call fails, returns exit code 1."""
        from pocketpaw.__main__ import check_openai_compatible
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:99999/v1",
            openai_compatible_model="model-x",
        )
        exit_code = await check_openai_compatible(settings)
        assert exit_code == 1

    async def test_success_with_tool_calling(self):
        """When API and tool calling succeed, returns exit code 0."""
        from pocketpaw.__main__ import check_openai_compatible
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="openai_compatible",
            openai_compatible_base_url="http://localhost:4000/v1",
            openai_compatible_model="model-x",
        )

        # Build OpenAI-format mock responses
        mock_msg1 = MagicMock()
        mock_msg1.content = "Hi there!"
        mock_msg1.tool_calls = [MagicMock()]  # has tool calls
        mock_choice1 = MagicMock()
        mock_choice1.message = mock_msg1
        mock_response1 = MagicMock()
        mock_response1.choices = [mock_choice1]

        mock_msg2 = MagicMock()
        mock_msg2.content = "4"
        mock_msg2.tool_calls = [MagicMock()]
        mock_choice2 = MagicMock()
        mock_choice2.message = mock_msg2
        mock_response2 = MagicMock()
        mock_response2.choices = [mock_choice2]

        mock_oc = MagicMock()
        mock_oc.chat.completions.create = AsyncMock(side_effect=[mock_response1, mock_response2])

        with patch(
            "pocketpaw.llm.client.LLMClient.create_openai_client",
            return_value=mock_oc,
        ):
            exit_code = await check_openai_compatible(settings)
            assert exit_code == 0
