"""Tests for dispatch logic and the persistent client in ClaudeAgentSDK.

Covers:
- Dispatch: all messages (SIMPLE, MODERATE, routing disabled) flow through
  the persistent Claude Code CLI path. The old direct-API ``_fast_chat``
  bypass was removed in 0.4.16 because it sidestepped the CLI's built-in
  conversation compaction and caused unrecoverable context-overflow errors
  on long sessions.
- Persistent ``ClaudeSDKClient`` reuse, reconnection, fallback, cleanup.
"""

from unittest.mock import MagicMock, patch

from pocketpaw.agents.claude_sdk import ClaudeAgentSDK
from pocketpaw.agents.model_router import ModelSelection, TaskComplexity

# Patch targets for local imports inside chat()
_LLM_CLIENT = "pocketpaw.llm.client.resolve_llm_client"
_MODEL_ROUTER = "pocketpaw.agents.model_router.ModelRouter"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides):
    """Create a minimal Settings-like object for tests."""
    defaults = {
        "agent_backend": "claude_agent_sdk",
        "tool_profile": "full",
        "tools_allow": [],
        "tools_deny": [],
        "smart_routing_enabled": True,
        "model_tier_simple": "claude-haiku-4-5-20251001",
        "model_tier_moderate": "claude-sonnet-4-5-20250929",
        "model_tier_complex": "claude-opus-4-6",
        "llm_provider": "anthropic",
        "anthropic_api_key": "sk-test-key",
        "anthropic_model": "claude-sonnet-4-5-20250929",
        "openai_api_key": "",
        "openai_model": "",
        "ollama_model": "",
        "ollama_host": "http://localhost:11434",
        "bypass_permissions": False,
    }
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


def _make_sdk(settings=None):
    """Create a ClaudeAgentSDK with mocked SDK imports."""
    s = settings or _make_settings()
    with patch("pocketpaw.agents.claude_sdk.ClaudeAgentSDK._initialize"):
        sdk = ClaudeAgentSDK(s)
    # Mark as available so chat() doesn't bail early
    sdk._sdk_available = True
    sdk._cli_available = True
    # Wire up types that _initialize normally sets from SDK imports
    sdk._HookMatcher = lambda matcher, hooks: MagicMock()
    sdk._ClaudeAgentOptions = lambda **kw: MagicMock()
    return sdk


class _FakeTextStream:
    """Async iterator that yields text chunks, simulating stream.text_stream."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


class _FakeStreamCM:
    """Fake async context manager for client.messages.stream()."""

    def __init__(self, chunks):
        self.text_stream = _FakeTextStream(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def get_final_message(self):
        return None


class _FakeSDKClient:
    """Fake ClaudeSDKClient for testing the persistent client path."""

    def __init__(self, responses=None, **_kwargs):
        self._responses = responses or []
        self.connected = False
        self.queries = []
        self.interrupted = False
        self.disconnected = False

    async def connect(self, prompt=None):
        self.connected = True

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_response(self):
        for msg in self._responses:
            yield msg

    async def receive_messages(self):
        for msg in self._responses:
            yield msg

    async def disconnect(self):
        self.connected = False
        self.disconnected = True

    async def interrupt(self):
        self.interrupted = True


# ---------------------------------------------------------------------------
# Tests for chat() dispatch logic
# ---------------------------------------------------------------------------


async def test_chat_dispatches_fast_path_for_simple():
    """SIMPLE messages now go through the persistent CLI path (fast-chat disabled)."""
    sdk = _make_sdk()

    # Create a fake response message — same setup as moderate path
    fake_msg = MagicMock()
    fake_msg.__class__.__name__ = "AssistantMessage"
    fake_msg.content = "simple response"

    fake_client = _FakeSDKClient(responses=[fake_msg])

    sdk._ClaudeSDKClient = lambda **kwargs: fake_client
    sdk._ClaudeAgentOptions = MagicMock()
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    sdk._AssistantMessage = None
    sdk._SystemMessage = None
    sdk._UserMessage = None
    sdk._ResultMessage = None

    selection = ModelSelection(
        complexity=TaskComplexity.SIMPLE,
        model="claude-haiku-4-5-20251001",
        reason="test",
    )

    with patch(_LLM_CLIENT) as mock_resolve:
        mock_llm = MagicMock()
        mock_llm.is_ollama = False
        mock_llm.is_openai_compatible = False
        mock_llm.is_gemini = False
        mock_llm.is_litellm = False
        mock_llm.is_openrouter = False
        mock_llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        mock_resolve.return_value = mock_llm

        with patch(_MODEL_ROUTER) as MockRouter:
            MockRouter.return_value.classify.return_value = selection
            with patch.object(ClaudeAgentSDK, "_get_mcp_servers", return_value={}):
                events = []
                async for ev in sdk.run("hi", system_prompt="identity"):
                    events.append(ev)

    # Simple messages now use the persistent client (same as moderate)
    assert fake_client.queries == ["hi"]
    assert any(e.type == "done" for e in events)


async def test_chat_uses_persistent_client_for_moderate():
    """chat() should use the persistent ClaudeSDKClient for MODERATE messages."""
    sdk = _make_sdk()

    # Create a fake response message
    fake_msg = MagicMock()
    fake_msg.__class__.__name__ = "AssistantMessage"
    fake_msg.content = "standard response"

    fake_client = _FakeSDKClient(responses=[fake_msg])

    sdk._ClaudeSDKClient = lambda **kwargs: fake_client
    sdk._ClaudeAgentOptions = MagicMock()
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    sdk._AssistantMessage = None
    sdk._SystemMessage = None
    sdk._UserMessage = None
    sdk._ResultMessage = None

    selection = ModelSelection(
        complexity=TaskComplexity.MODERATE,
        model="claude-sonnet-4-5-20250929",
        reason="test",
    )

    with patch(_LLM_CLIENT) as mock_resolve:
        mock_llm = MagicMock()
        mock_llm.is_ollama = False
        mock_llm.is_openai_compatible = False
        mock_llm.is_gemini = False
        mock_llm.is_litellm = False
        mock_llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        mock_resolve.return_value = mock_llm

        with patch(_MODEL_ROUTER) as MockRouter:
            MockRouter.return_value.classify.return_value = selection
            with patch.object(ClaudeAgentSDK, "_get_mcp_servers", return_value={}):
                events = []
                async for ev in sdk.run("analyze this code", system_prompt="identity"):
                    events.append(ev)

    # Client was used (connected then disconnected by cleanup since no ResultMessage)
    assert fake_client.queries == ["analyze this code"]
    assert any(e.type == "done" for e in events)


async def test_chat_standard_path_when_routing_disabled():
    """With smart_routing_enabled=False, chat() should use the standard path."""
    sdk = _make_sdk(_make_settings(smart_routing_enabled=False))

    fake_msg = MagicMock()
    fake_msg.__class__.__name__ = "ResultMessage"
    fake_msg.is_error = False
    fake_msg.result = "done"

    fake_client = _FakeSDKClient(responses=[fake_msg])

    sdk._ClaudeSDKClient = lambda **kwargs: fake_client
    sdk._ClaudeAgentOptions = MagicMock()
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    sdk._AssistantMessage = None
    sdk._SystemMessage = None
    sdk._UserMessage = None
    sdk._ResultMessage = type(fake_msg)

    with patch(_LLM_CLIENT) as mock_resolve:
        mock_llm = MagicMock()
        mock_llm.is_ollama = False
        mock_llm.is_openai_compatible = False
        mock_llm.is_gemini = False
        mock_llm.is_litellm = False
        mock_llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        mock_resolve.return_value = mock_llm

        with patch.object(ClaudeAgentSDK, "_get_mcp_servers", return_value={}):
            events = []
            async for ev in sdk.run("hi", system_prompt="identity"):
                events.append(ev)

    # "hi" would be SIMPLE, but routing is disabled -> standard path via persistent client
    assert any(e.type == "done" for e in events)


# ---------------------------------------------------------------------------
# Tests for persistent ClaudeSDKClient
# ---------------------------------------------------------------------------


async def test_persistent_client_reuse():
    """Subsequent calls with same options should reuse the existing client."""
    sdk = _make_sdk()

    clients_created = []

    def _client_factory(**kwargs):
        c = _FakeSDKClient()
        clients_created.append(c)
        return c

    sdk._ClaudeSDKClient = _client_factory

    options1 = MagicMock()
    options1.model = "claude-sonnet-4-5-20250929"
    options1.allowed_tools = ["Bash", "Read"]

    # First call — creates client
    client1 = await sdk._get_or_create_client(options1)
    assert len(clients_created) == 1
    assert client1.connected

    # Second call with same options — reuses client
    options2 = MagicMock()
    options2.model = "claude-sonnet-4-5-20250929"
    options2.allowed_tools = ["Bash", "Read"]

    client2 = await sdk._get_or_create_client(options2)
    assert len(clients_created) == 1  # No new client created
    assert client2 is client1


async def test_persistent_client_reconnects_on_model_change():
    """Changing the model should disconnect old client and create a new one."""
    sdk = _make_sdk()

    clients_created = []

    def _client_factory(**kwargs):
        c = _FakeSDKClient()
        clients_created.append(c)
        return c

    sdk._ClaudeSDKClient = _client_factory

    options1 = MagicMock()
    options1.model = "claude-sonnet-4-5-20250929"
    options1.allowed_tools = ["Bash"]

    # First call — creates client
    client1 = await sdk._get_or_create_client(options1)
    assert len(clients_created) == 1

    # Second call with different model — creates new client
    options2 = MagicMock()
    options2.model = "claude-haiku-4-5-20251001"
    options2.allowed_tools = ["Bash"]

    client2 = await sdk._get_or_create_client(options2)
    assert len(clients_created) == 2
    assert client2 is not client1
    assert client1.disconnected  # Old client was disconnected


async def test_persistent_client_falls_back_to_query():
    """If the persistent client fails, chat() should fall back to stateless query()."""
    sdk = _make_sdk()

    def _broken_factory(**kwargs):
        raise RuntimeError("client creation failed")

    sdk._ClaudeSDKClient = _broken_factory

    # Set up stateless query as fallback
    fallback_called = False

    async def _fake_query(*, prompt, options):
        nonlocal fallback_called
        fallback_called = True
        msg = MagicMock()
        msg.__class__.__name__ = "ResultMessage"
        sdk._ResultMessage = type(msg)
        msg.is_error = False
        msg.result = "done"
        yield msg

    sdk._query = _fake_query
    sdk._ClaudeAgentOptions = MagicMock()
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    sdk._AssistantMessage = None
    sdk._SystemMessage = None
    sdk._UserMessage = None
    sdk._ResultMessage = None

    selection = ModelSelection(
        complexity=TaskComplexity.MODERATE,
        model="claude-sonnet-4-5-20250929",
        reason="test",
    )

    with patch(_LLM_CLIENT) as mock_resolve:
        mock_llm = MagicMock()
        mock_llm.is_ollama = False
        mock_llm.is_openai_compatible = False
        mock_llm.is_gemini = False
        mock_llm.is_litellm = False
        mock_llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        mock_resolve.return_value = mock_llm

        with patch(_MODEL_ROUTER) as MockRouter:
            MockRouter.return_value.classify.return_value = selection
            with patch.object(ClaudeAgentSDK, "_get_mcp_servers", return_value={}):
                events = []
                async for ev in sdk.run("test message", system_prompt="identity"):
                    events.append(ev)

    assert fallback_called
    assert any(e.type == "done" for e in events)


async def test_stop_interrupts_persistent_client():
    """stop() should call interrupt() on the persistent client."""
    sdk = _make_sdk()

    fake_client = _FakeSDKClient()
    fake_client.connected = True
    sdk._client = fake_client
    sdk._client_options_key = "test"

    await sdk.stop()

    assert sdk._stop_flag
    assert fake_client.interrupted
    assert fake_client.disconnected


async def test_cleanup_disconnects_client():
    """cleanup() should disconnect and clear the persistent client."""
    sdk = _make_sdk()

    fake_client = _FakeSDKClient()
    fake_client.connected = True
    sdk._client = fake_client
    sdk._client_options_key = "test:key"

    await sdk.cleanup()

    assert sdk._client is None
    assert sdk._client_options_key is None
    assert fake_client.disconnected


async def test_cleanup_noop_when_no_client():
    """cleanup() should be safe to call when no client exists."""
    sdk = _make_sdk()
    assert sdk._client is None

    # Should not raise
    await sdk.cleanup()
    assert sdk._client is None
