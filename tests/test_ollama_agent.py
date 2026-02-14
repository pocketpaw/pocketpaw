"""Tests for Ollama integration across both agent backends."""

from unittest.mock import AsyncMock, MagicMock, patch

from pocketclaw.llm.client import resolve_llm_client

# ---------------------------------------------------------------------------
# PocketPaw Native + Ollama
# ---------------------------------------------------------------------------


class TestNativeOllamaInit:
    """Verify PocketPaw Native initializes correctly for Ollama."""

    @patch("anthropic.AsyncAnthropic")
    def test_ollama_provider_creates_client(self, mock_anthropic):
        """When llm_provider='ollama', client uses ollama_host as base_url."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="ollama",
            ollama_host="http://localhost:11434",
            ollama_model="qwen2.5:7b",
        )
        orch = PocketPawOrchestrator(settings)

        mock_anthropic.assert_called_once_with(
            base_url="http://localhost:11434",
            api_key="ollama",
            timeout=120.0,
            max_retries=1,
        )
        assert orch._llm.is_ollama
        assert orch._client is not None

    @patch("anthropic.AsyncAnthropic")
    def test_anthropic_provider_creates_client(self, mock_anthropic):
        """When llm_provider='anthropic', client uses anthropic_api_key."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="anthropic",
            anthropic_api_key="sk-test-key",
        )
        orch = PocketPawOrchestrator(settings)

        mock_anthropic.assert_called_once_with(
            api_key="sk-test-key",
            timeout=60.0,
            max_retries=2,
        )
        assert orch._llm.is_anthropic

    @patch("anthropic.AsyncAnthropic")
    def test_auto_fallback_to_ollama(self, mock_anthropic):
        """When provider='auto' and no API key, falls back to Ollama."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="auto",
            anthropic_api_key=None,
            ollama_host="http://myhost:11434",
        )
        orch = PocketPawOrchestrator(settings)

        mock_anthropic.assert_called_once_with(
            base_url="http://myhost:11434",
            api_key="ollama",
            timeout=120.0,
            max_retries=1,
        )
        assert orch._llm.is_ollama

    @patch("anthropic.AsyncAnthropic")
    def test_auto_prefers_anthropic(self, mock_anthropic):
        """When provider='auto' and API key exists, uses Anthropic."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="auto",
            anthropic_api_key="sk-real-key",
        )
        orch = PocketPawOrchestrator(settings)

        mock_anthropic.assert_called_once_with(
            api_key="sk-real-key",
            timeout=60.0,
            max_retries=2,
        )
        assert orch._llm.is_anthropic

    def test_no_provider_available(self):
        """When provider='anthropic' but no key, client stays None."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="anthropic",
            anthropic_api_key=None,
        )
        orch = PocketPawOrchestrator(settings)

        assert orch._client is None


class TestNativeOllamaModel:
    """Verify the correct model is used for Ollama in chat()."""

    @patch("anthropic.AsyncAnthropic")
    async def test_ollama_uses_ollama_model(self, mock_anthropic_cls):
        """When provider is Ollama, chat() uses ollama_model."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="ollama",
            ollama_model="qwen2.5:7b",
            anthropic_model="claude-sonnet-4-5-20250514",
        )

        # Mock the client's messages.create to return a simple response
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="Hello!")]
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_cls.return_value = mock_client

        orch = PocketPawOrchestrator(settings)
        assert orch._llm.is_ollama

        # Collect events from chat()
        events = []
        async for event in orch.chat("Hi", system_prompt="You are helpful"):
            events.append(event)

        # Verify the model passed to messages.create
        call_kwargs = mock_client.messages.create.call_args
        assert call_kwargs.kwargs["model"] == "qwen2.5:7b"


class TestNativeOllamaSmartRouting:
    """Smart routing should be skipped for Ollama."""

    @patch("anthropic.AsyncAnthropic")
    async def test_smart_routing_skipped_for_ollama(self, mock_anthropic_cls):
        """When provider is Ollama, smart routing is not invoked."""
        from pocketclaw.agents.pocketpaw_native import PocketPawOrchestrator
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="ollama",
            ollama_model="qwen2.5:7b",
            smart_routing_enabled=True,
        )

        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="Hello!")]
        mock_response.stop_reason = "end_turn"
        mock_client.messages.create = AsyncMock(return_value=mock_response)
        mock_anthropic_cls.return_value = mock_client

        orch = PocketPawOrchestrator(settings)

        with patch("pocketclaw.agents.model_router.ModelRouter") as mock_router_cls:
            events = []
            async for event in orch.chat("Hi"):
                events.append(event)
            # ModelRouter should NOT have been instantiated
            mock_router_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Claude SDK + Ollama
# ---------------------------------------------------------------------------


class TestClaudeSDKOllamaLogic:
    """Test Ollama provider detection logic using LLMClient.

    Instead of trying to mock the complex SDK initialization, we test
    the provider selection logic via resolve_llm_client directly.
    """

    def test_ollama_provider_detection(self):
        """Verify Ollama is resolved."""
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="ollama",
            ollama_host="http://localhost:11434",
            ollama_model="mistral:7b",
        )
        llm = resolve_llm_client(settings)
        assert llm.is_ollama

    def test_auto_without_key_detects_ollama(self):
        """When provider='auto' and no API key, Ollama is detected."""
        from pocketclaw.config import Settings

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
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="auto",
            anthropic_api_key="sk-test",
        )
        llm = resolve_llm_client(settings)
        assert llm.is_anthropic

    def test_ollama_env_vars_construction(self):
        """Verify the env dict that would be passed to ClaudeAgentOptions."""
        from pocketclaw.config import Settings

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
        from pocketclaw.config import Settings

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
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="ollama",
            smart_routing_enabled=True,
        )
        llm = resolve_llm_client(settings)
        should_route = settings.smart_routing_enabled and not llm.is_ollama
        assert should_route is False

    def test_smart_routing_enabled_for_anthropic(self):
        """Verify smart routing is not skipped for Anthropic."""
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="anthropic",
            anthropic_api_key="sk-test",
            smart_routing_enabled=True,
        )
        llm = resolve_llm_client(settings)
        should_route = settings.smart_routing_enabled and not llm.is_ollama
        assert should_route is True


# ---------------------------------------------------------------------------
# Router — Ollama detection
# ---------------------------------------------------------------------------


class TestRouterOllamaDetection:
    """Verify router logs Ollama detection."""

    def test_ollama_detection_logged(self):
        """When llm_provider='ollama', router logs a message."""
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="ollama",
            ollama_host="http://localhost:11434",
            agent_backend="open_interpreter",
        )

        with (
            patch("pocketclaw.agents.router.logger") as mock_logger,
            patch("pocketclaw.agents.open_interpreter.OpenInterpreterAgent"),
        ):
            try:
                from pocketclaw.agents.router import AgentRouter

                AgentRouter(settings)
            except Exception:
                pass  # import may fail in test env, that's fine

            calls = [str(c) for c in mock_logger.info.call_args_list]
            ollama_logged = any("Ollama provider detected" in c for c in calls)
            assert ollama_logged, f"Expected Ollama log message, got: {calls}"

    def test_auto_detection_with_no_keys(self):
        """When provider='auto' and no API keys, Ollama is detected."""
        from pocketclaw.config import Settings

        settings = Settings(
            llm_provider="auto",
            anthropic_api_key=None,
            openai_api_key=None,
            ollama_host="http://localhost:11434",
            agent_backend="open_interpreter",
        )

        with (
            patch("pocketclaw.agents.router.logger") as mock_logger,
            patch("pocketclaw.agents.open_interpreter.OpenInterpreterAgent"),
        ):
            try:
                from pocketclaw.agents.router import AgentRouter

                AgentRouter(settings)
            except Exception:
                pass

            calls = [str(c) for c in mock_logger.info.call_args_list]
            ollama_logged = any("Ollama provider detected" in c for c in calls)
            assert ollama_logged


# ---------------------------------------------------------------------------
# check_ollama CLI
# ---------------------------------------------------------------------------


class TestCheckOllama:
    """Tests for the --check-ollama CLI command."""

    async def test_server_unreachable_returns_1(self):
        """When Ollama server is down, check returns exit code 1."""
        from pocketclaw.__main__ import check_ollama
        from pocketclaw.config import Settings

        settings = Settings(
            ollama_host="http://localhost:99999",  # unreachable port
            ollama_model="llama3.2",
        )

        exit_code = await check_ollama(settings)
        assert exit_code == 1

    async def test_server_reachable_model_missing(self):
        """When server is up but model not found, warns."""
        import httpx

        from pocketclaw.__main__ import check_ollama
        from pocketclaw.config import Settings

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
                "pocketclaw.llm.client.LLMClient.create_anthropic_client",
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
