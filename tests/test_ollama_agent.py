"""Tests for Ollama integration."""

from unittest.mock import AsyncMock, MagicMock, patch

from pocketpaw.llm.client import resolve_llm_client

# ---------------------------------------------------------------------------
# Claude SDK + Ollama (via LLMClient)
# ---------------------------------------------------------------------------


class TestClaudeSDKOllamaLogic:
    """Test Ollama provider detection logic using LLMClient.

    Instead of trying to mock the complex SDK initialization, we test
    the provider selection logic via resolve_llm_client directly.
    """

    def test_ollama_provider_detection(self):
        """Verify Ollama is resolved."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="ollama",
            ollama_host="http://localhost:11434",
            ollama_model="mistral:7b",
        )
        llm = resolve_llm_client(settings)
        assert llm.is_ollama

    def test_auto_without_key_detects_ollama(self):
        """When provider='auto' and no API key, Ollama is detected."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="auto",
            anthropic_api_key=None,
            ollama_host="http://localhost:11434",
            ollama_model="mistral:7b",
        )
        llm = resolve_llm_client(settings)
        assert llm.is_ollama

    def test_auto_with_key_uses_anthropic(self):
        """When provider='auto' and API key exists, Anthropic is used."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="auto",
            anthropic_api_key="sk-test",
        )
        llm = resolve_llm_client(settings)
        assert llm.is_anthropic

    def test_ollama_env_vars_construction(self):
        """Verify the env dict that would be passed to ClaudeAgentOptions."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="ollama",
            ollama_host="http://myhost:11434",
            ollama_model="llama3.2",
        )

        llm = resolve_llm_client(settings)
        env = llm.to_sdk_env()

        assert env["ANTHROPIC_BASE_URL"] == "http://myhost:11434"
        assert env["ANTHROPIC_API_KEY"] == "ollama"
        assert "ANTHROPIC_AUTH_TOKEN" not in env
        assert llm.model == "llama3.2"

    def test_anthropic_env_vars_construction(self):
        """Verify the env dict for Anthropic provider."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="anthropic",
            anthropic_api_key="sk-real-key",
        )

        llm = resolve_llm_client(settings)
        env = llm.to_sdk_env()

        assert env.get("ANTHROPIC_API_KEY") == "sk-real-key"
        assert "ANTHROPIC_BASE_URL" not in env

    def test_smart_routing_skipped_for_ollama(self):
        """Verify smart routing skip condition for Ollama."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="ollama",
            smart_routing_enabled=True,
        )
        llm = resolve_llm_client(settings)
        should_route = settings.smart_routing_enabled and not llm.is_ollama
        assert should_route is False

    def test_smart_routing_enabled_for_anthropic(self):
        """Verify smart routing is not skipped for Anthropic."""
        from pocketpaw.config import Settings

        settings = Settings(
            llm_provider="anthropic",
            anthropic_api_key="sk-test",
            smart_routing_enabled=True,
        )
        llm = resolve_llm_client(settings)
        should_route = settings.smart_routing_enabled and not llm.is_ollama
        assert should_route is True




# ---------------------------------------------------------------------------
# check_ollama CLI
# ---------------------------------------------------------------------------


class TestCheckOllama:
    """Tests for the --check-ollama CLI command."""

    async def test_server_unreachable_returns_1(self):
        """When Ollama server is down, check returns exit code 1."""
        from pocketpaw.__main__ import check_ollama
        from pocketpaw.config import Settings

        settings = Settings(
            ollama_host="http://localhost:99999",  # unreachable port
            ollama_model="llama3.2",
        )

        exit_code = await check_ollama(settings)
        assert exit_code == 1

    async def test_server_reachable_model_missing(self):
        """When server is up but model not found, warns."""
        import httpx

        from pocketpaw.__main__ import check_ollama
        from pocketpaw.config import Settings

        # Mock httpx response for /api/tags
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"models": [{"name": "llama3.2:latest"}]}
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        settings = Settings(
            ollama_host="http://localhost:11434",
            ollama_model="nonexistent-model",
        )

        # Patch httpx client and the Anthropic client returned by create_anthropic_client
        with (
            patch.object(httpx, "AsyncClient", return_value=mock_client),
            patch(
                "pocketpaw.llm.client.LLMClient.create_anthropic_client",
            ) as mock_create,
        ):
            mock_ac = MagicMock()
            mock_ac.messages.create = AsyncMock(
                side_effect=Exception("model not found"),
            )
            mock_create.return_value = mock_ac

            exit_code = await check_ollama(settings)
            # Model not found + API failure = exit code 1
            assert exit_code == 1
