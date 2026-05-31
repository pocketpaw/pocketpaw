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


# ---------------------------------------------------------------------------
# Concurrency lease bug reproduction tests
# ---------------------------------------------------------------------------
# ClaudeSDKBackend.run() is one async generator instance shared across every
# concurrent session of an agent. It dispatches on the bool _client_in_use.
# A run that never acquired the lease (a stateless-fallback run) must not
# mutate the shared lease or the shared persistent client on ANY exit path.
#
# These tests are DETERMINISTIC — no real subprocesses, no asyncio.sleep,
# no actual concurrency. They drive the state machine directly with fakes,
# the same way the dispatch tests above do.
# ---------------------------------------------------------------------------


async def test_stateless_fallback_does_not_clear_sibling_lease():
    """Stateless-fallback run must NOT clear _client_in_use=True set by a sibling.

    Scenario:
      1. A persistent run is in flight — it owns the lease (_client_in_use=True).
      2. A second concurrent message arrives; because the flag is True it takes
         the stateless-fallback path through _resilient_query().
      3. The stateless run finishes (generator drained).
      4. BUG: the finally block unconditionally sets _client_in_use=False,
         clearing the sibling's lease.

    Expected (correct): _client_in_use is still True after the stateless run.
    Actual (buggy):     _client_in_use is False — the lease was stolen.
    """
    sdk = _make_sdk()

    # ── Simulate a sibling persistent run holding the lease ──────────────────
    sdk._client_in_use = True

    # ── Wire a fake stateless query that yields one AssistantMessage ─────────
    # This is the path run() takes when _client_in_use is True on entry.
    stateless_called = False

    async def _fake_stateless_query(*, prompt, options):
        nonlocal stateless_called
        stateless_called = True
        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = "fallback response"
        yield msg

    sdk._query = _fake_stateless_query
    sdk._ClaudeAgentOptions = MagicMock()
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    sdk._AssistantMessage = None  # Disable isinstance branch — treated as unknown event
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
        mock_llm.is_openrouter = False
        mock_llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        mock_resolve.return_value = mock_llm

        with patch(_MODEL_ROUTER) as MockRouter:
            MockRouter.return_value.classify.return_value = selection
            with patch.object(ClaudeAgentSDK, "_get_mcp_servers", return_value={}):
                events = []
                async for ev in sdk.run("concurrent message", system_prompt="identity"):
                    events.append(ev)

    # The stateless path must have been taken (because _client_in_use was True)
    assert stateless_called, "Expected stateless fallback path to be used"

    # The run should complete cleanly
    assert any(e.type == "done" for e in events)

    # ── THE BUG: this assertion FAILS against current code ───────────────────
    # The stateless run never acquired the lease, so it must not clear it.
    assert sdk._client_in_use is True, (
        "BUG REPRODUCED: stateless-fallback run cleared _client_in_use=True "
        "that belonged to a sibling persistent run. The finally block must be "
        "guarded: only clear the flag when this run actually acquired it."
    )


async def test_stateless_fallback_does_not_destroy_shared_client():
    """Stateless-fallback run must NOT disconnect the shared _client subprocess.

    Scenario:
      1. A persistent run owns the lease and has an active _client subprocess
         (_client_in_use=True, sdk._client=<fake_client>).
      2. A stateless-fallback run executes (because _client_in_use was True).
         Its stream ends WITHOUT a ResultMessage (simulating an interrupted
         or plain-text response that has no ResultMessage sentinel).
      3. BUG concern: the teardown guard checks
             ``_persistent_client is not None and not _saw_result``
         On the stateless path _persistent_client stays None, so the guard
         SHOULD be safe — this test pins that invariant.

    sdk._client must still be the original fake client object after the
    stateless run finishes (not None, not a different object).
    """
    sdk = _make_sdk()

    # ── Simulate a sibling persistent run: owns lease AND has live client ────
    fake_sibling_client = _FakeSDKClient()
    fake_sibling_client.connected = True
    sdk._client = fake_sibling_client
    sdk._client_options_key = "sibling:session"
    sdk._client_in_use = True  # sibling holds the lease

    # ── Stateless query that finishes WITHOUT a ResultMessage ─────────────────
    async def _fake_stateless_no_result(*, prompt, options):
        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = "response without result message"
        yield msg
        # Deliberately NO ResultMessage yielded

    sdk._query = _fake_stateless_no_result
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
        mock_llm.is_openrouter = False
        mock_llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        mock_resolve.return_value = mock_llm

        with patch(_MODEL_ROUTER) as MockRouter:
            MockRouter.return_value.classify.return_value = selection
            with patch.object(ClaudeAgentSDK, "_get_mcp_servers", return_value={}):
                events = []
                async for ev in sdk.run("concurrent message 2", system_prompt="identity"):
                    events.append(ev)

    # The run must complete cleanly
    assert any(e.type == "done" for e in events)

    # The sibling's persistent client must NOT have been disconnected or nulled
    # by the stateless run's teardown.
    assert sdk._client is fake_sibling_client, (
        "BUG: stateless-fallback run destroyed sdk._client belonging to a "
        "sibling persistent run. The teardown guard must only fire when this "
        "run owns the persistent client."
    )
    assert not fake_sibling_client.disconnected, (
        "BUG: stateless-fallback run called disconnect() on the sibling's client."
    )


async def test_stateless_fallback_error_path_does_not_clear_sibling_lease():
    """A stateless-fallback run that errors hard must NOT clear a sibling's lease.

    Same bug class as test_stateless_fallback_does_not_clear_sibling_lease, but
    on the error exit path instead of the finally path.

    Scenario:
      1. A persistent run owns the lease and has a live _client subprocess
         (_client_in_use=True, sdk._client=<fake_client>).
      2. A second concurrent message arrives; the flag is True so it takes the
         stateless-fallback path.
      3. The stateless stream raises a hard exception (a plain RuntimeError —
         NOT a MessageParseError, so _resilient_query re-raises it and it
         escapes to run()'s outer ``except Exception`` handler).
      4. BUG: that handler clears self._client_in_use and nulls self._client
         unconditionally, stealing the sibling persistent run's lease and
         destroying its subprocess.

    Expected (correct): _client_in_use stays True and _client is untouched —
    the failing run never acquired the lease, so it must not release it.
    """
    sdk = _make_sdk()

    # ── Simulate a sibling persistent run: owns lease AND has live client ────
    fake_sibling_client = _FakeSDKClient()
    fake_sibling_client.connected = True
    sdk._client = fake_sibling_client
    sdk._client_options_key = "sibling:session"
    sdk._client_in_use = True  # sibling holds the lease

    # ── Stateless query that yields once, then raises a hard exception ───────
    # A plain RuntimeError is not a MessageParseError, so _resilient_query
    # re-raises it and it escapes to run()'s outer ``except Exception``.
    async def _fake_stateless_raises(*, prompt, options):
        msg = MagicMock()
        msg.__class__.__name__ = "AssistantMessage"
        msg.content = "partial response before crash"
        yield msg
        raise RuntimeError("simulated hard CLI failure")

    sdk._query = _fake_stateless_raises
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
        mock_llm.is_openrouter = False
        mock_llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        mock_resolve.return_value = mock_llm

        with patch(_MODEL_ROUTER) as MockRouter:
            MockRouter.return_value.classify.return_value = selection
            with patch.object(ClaudeAgentSDK, "_get_mcp_servers", return_value={}):
                events = []
                async for ev in sdk.run("concurrent message 3", system_prompt="identity"):
                    events.append(ev)

    # The run hit the error path — it must surface an error event.
    assert any(e.type == "error" for e in events)

    # ── THE BUG: the outer except handler clears the lease unconditionally ───
    # The failing run never acquired the lease, so it must leave it alone.
    assert sdk._client_in_use is True, (
        "BUG REPRODUCED: stateless-fallback run that errored hard cleared "
        "_client_in_use=True belonging to a sibling persistent run. The outer "
        "except handler must only clear the flag when this run acquired it."
    )

    # The sibling's persistent client must NOT have been nulled or disconnected.
    assert sdk._client is fake_sibling_client, (
        "BUG: stateless-fallback run that errored hard destroyed sdk._client "
        "belonging to a sibling persistent run."
    )
    assert not fake_sibling_client.disconnected, (
        "BUG: stateless-fallback error path called disconnect() on the sibling's client."
    )


async def test_bun_crash_retry_starts_with_clean_lease_state():
    """A persistent run that owns the lease and hits a Bun crash retries cleanly.

    Scenario:
      1. A run takes the persistent path — it acquires the lease
         (acquired_lease=True, _client_in_use=True).
      2. The persistent stream raises an "exit code" error and CLI stderr
         carries a Bun-crash hint, so run()'s outer except handler classifies
         it as a Bun crash and triggers the recursive self.run(...) retry.
      3. Because this run owned the lease, the ownership gate already cleared
         _client and set _client_in_use=False before the retry runs.

    Invariants pinned:
      - The retry sees a clean lease (_client_in_use=False on entry), so it
        takes the persistent path itself rather than a stale stateless
        fallback.
      - The lease is neither leaked (stuck True with no owner) nor
        double-released — after the whole run finishes it is False.
      - The run ultimately completes (a "done" event is emitted).
    """
    sdk = _make_sdk()

    # ── Real options factory so the crashing client can reach the stderr
    # callback run() wires into options_kwargs["stderr"]. ────────────────────
    class _Options:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)
            self.model = kwargs.get("model", "")
            self.allowed_tools = kwargs.get("allowed_tools", [])

    sdk._ClaudeAgentOptions = _Options

    class _ResultMsg:
        """Minimal stand-in for the SDK ResultMessage type."""

        def __init__(self):
            self.is_error = False
            self.result = "recovered after retry"
            self.total_cost_usd = None
            self.usage = {}

    sdk._ResultMessage = _ResultMsg

    class _BunCrashClient:
        """Persistent client whose stream raises a Bun-style crash."""

        def __init__(self, options=None, **_kw):
            self.options = options
            self.connected = False
            self.disconnected = False
            self.queries: list[str] = []

        async def connect(self, prompt=None):
            self.connected = True

        async def query(self, prompt, session_id="default"):
            self.queries.append(prompt)

        async def receive_messages(self):
            # Feed a Bun-crash hint through the same stderr callback the SDK
            # would use, then raise an "exit code" error so run() classifies
            # this as a Bun crash.
            stderr_cb = getattr(self.options, "stderr", None)
            if stderr_cb is not None:
                stderr_cb("bun has crashed: switch on corrupt value")
            raise RuntimeError("Claude CLI exited with exit code 3")
            yield  # pragma: no cover — makes this an async generator

        async def disconnect(self):
            self.connected = False
            self.disconnected = True

        async def interrupt(self):
            pass

    class _CleanClient:
        """Persistent client the retry uses — streams a ResultMessage."""

        def __init__(self, options=None, **_kw):
            self.options = options
            self.connected = False
            self.disconnected = False
            self.queries: list[str] = []

        async def connect(self, prompt=None):
            self.connected = True

        async def query(self, prompt, session_id="default"):
            self.queries.append(prompt)

        async def receive_messages(self):
            yield _ResultMsg()

    created: list[object] = []

    def _client_factory(**kwargs):
        # First persistent client crashes (Bun-style); the retry gets a
        # clean client that streams a ResultMessage.
        client = _BunCrashClient(**kwargs) if not created else _CleanClient(**kwargs)
        created.append(client)
        return client

    sdk._ClaudeSDKClient = _client_factory

    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    sdk._AssistantMessage = None
    sdk._SystemMessage = None
    sdk._UserMessage = None

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
        mock_llm.is_openrouter = False
        mock_llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        mock_resolve.return_value = mock_llm

        with patch(_MODEL_ROUTER) as MockRouter:
            MockRouter.return_value.classify.return_value = selection
            with patch.object(ClaudeAgentSDK, "_get_mcp_servers", return_value={}):
                events = []
                async for ev in sdk.run("crash then recover", system_prompt="identity"):
                    events.append(ev)

    # The Bun crash was detected — a "status" retry event was emitted.
    assert any(e.type == "status" for e in events), (
        "Expected a Bun-crash retry status event — the crash was not classified."
    )

    # The retry took the PERSISTENT path, not a stale stateless fallback.
    # run() only builds a second ClaudeSDKClient if the retry's dispatch
    # check saw _client_in_use=False. A leaked lease would have left the
    # flag True, sending the retry through _resilient_query() instead — and
    # _CleanClient would never be constructed. So a second created client
    # is direct proof the owning run released its lease on the error path.
    assert len(created) == 2, (
        "BUG: the Bun-crash retry did not take the persistent path — the "
        "owning run leaked its lease instead of releasing it on the error "
        f"path. Clients created: {len(created)}."
    )
    assert isinstance(created[0], _BunCrashClient)
    assert isinstance(created[1], _CleanClient)

    # The retry actually sent the query on the fresh client.
    assert created[1].queries == ["crash then recover"]

    # The run ultimately completed.
    assert any(e.type == "done" for e in events)

    # Lease is not stuck after the whole run — neither leaked nor
    # double-released. The retry acquired it, then released it on its own
    # clean exit; nothing is left holding it.
    assert sdk._client_in_use is False, (
        "BUG: lease left True after the run finished — leaked or not released."
    )
