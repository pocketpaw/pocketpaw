# tests/test_claude_sdk_warm_lease.py
# Updated: 2026-07-01 (fix/warm-reuse session_id) — added the session_id-capture
#   suite (section 6) that pins the fix for the live WARM NO-OP: SS-1 captured the
#   native session_id ONLY from the init SystemMessage, which never surfaced on the
#   leased supervised-fresh client, so owns_capture stayed True and warm_reuse never
#   fired. Three tests: (1) REPRODUCTION — a quirk stream (init SystemMessage with
#   NO session_id, then a normal assistant message, then a ResultMessage carrying
#   the id) surfaces exactly one session_id event via the ResultMessage fallback
#   (pre-fix: zero events); (2) NO-DOUBLE-EMIT — both sources carry the id → exactly
#   one event (SystemMessage wins, emit-once gate skips the ResultMessage); (3)
#   LEGACY — session_handle=None → no session_id event from either source.
# Created: 2026-06-30 (feat/warm-reuse WH-1) — pins the BACKEND half of the WARM
# perf tier: ``ClaudeSDKBackend.run`` can drive a turn against a caller-LEASED
# warm ``ClaudeSDKClient`` (owned by the SessionSupervisor, WH-2/WH-3) instead of
# always building / caching its own ``self._client``. Turn 2+ then reuses the live
# subprocess instead of resuming COLD.
#
# Live ``claude`` model calls are BLOCKED here, so these tests SPY on the
# construction/stream boundary (a fake ``ClaudeSDKClient`` recording connect /
# disconnect / query, plus a capturing options factory) rather than making a real
# call. They assert the four run paths plus the busy edge:
#   1. WARM REUSE — a ``warm_client`` whose ``options_key`` MATCHES this turn drives
#      the leased client's ``query`` directly: no ``connect``, no second client, no
#      ``resume``, and the raw message (no injected history block) is sent.
#   2. KEY MISMATCH — a stale ``warm_client`` + ``on_client_built`` builds a FRESH
#      client and hands ``on_client_built`` the NEW key (not the stale one).
#   3. SUPERVISED FRESH — no ``warm_client`` but ``on_client_built`` set →
#      ``on_client_built(client, key, teardown)`` is invoked; ``resume`` is set on
#      the options ONLY when ``session_handle.cli_session_id`` is present.
#   4. LEGACY — neither param → the ``self._client`` warm path, unchanged
#      (``on_client_built`` never called, ``self._client`` cached as before).
#   5. BUSY edge — a matching but ``busy`` lease falls back to a fresh stateless
#      query for the turn and does NOT re-drive or rebind the leased client.

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pocketpaw.agents.backend import LeasedClient, SessionHandle
from pocketpaw.agents.claude_sdk import ClaudeAgentSDK, ClaudeSDKBackend
from pocketpaw.agents.model_router import ModelSelection, TaskComplexity

_LLM_CLIENT = "pocketpaw.llm.client.resolve_llm_client"
_MODEL_ROUTER = "pocketpaw.agents.model_router.ModelRouter"


# ===========================================================================
# Fakes
# ===========================================================================


def _make_settings(**overrides):
    defaults = {
        "agent_backend": "claude_agent_sdk",
        "tool_profile": "full",
        "tools_allow": [],
        "tools_deny": [],
        "smart_routing_enabled": False,
        "claude_sdk_provider": "anthropic",
        "claude_sdk_model": None,
        "claude_sdk_max_turns": None,
        "sdk_load_bundled_skills": False,
        "anthropic_api_key": "sk-test-key",
    }
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


class _Options:
    """Real options stand-in so ``_client_cache_key`` / dispatch can read
    ``system_prompt`` / ``model`` / ``allowed_tools`` / ``plugins`` / ``resume``."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.model = kwargs.get("model", "")
        self.allowed_tools = kwargs.get("allowed_tools", [])
        self.system_prompt = kwargs.get("system_prompt", "")
        self.plugins = kwargs.get("plugins", [])
        self.cwd = kwargs.get("cwd", "")
        self.resume = kwargs.get("resume", None)


def _capturing_options(sink: list[_Options]):
    def _factory(**kwargs):
        opt = _Options(**kwargs)
        sink.append(opt)
        return opt

    return _factory


class _FakeSystemMessage:
    def __init__(self, subtype: str, data: dict):
        self.subtype = subtype
        self.data = data


class _FakeResultMsg:
    def __init__(self):
        self.is_error = False
        self.result = "ok"
        self.total_cost_usd = None
        self.usage = {}


class _SpyClient:
    """Spy ``ClaudeSDKClient``. Records connect / disconnect / query so a test can
    prove which path drove it. ``receive_messages()`` yields the SDK init/system
    message then a ResultMessage."""

    def __init__(self, options=None, *, init_session_id="sess-init", **_kw):
        self.options = options
        self._init_session_id = init_session_id
        self.queries: list[str] = []
        self.connect_count = 0
        self.disconnect_count = 0

    async def connect(self, prompt=None):
        self.connect_count += 1

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_messages(self):
        yield _FakeSystemMessage(subtype="init", data={"session_id": self._init_session_id})
        yield _FakeResultMsg()

    async def disconnect(self):
        self.disconnect_count += 1

    async def interrupt(self):
        pass


def _capturing_clients(sink: list[_SpyClient]):
    def _factory(**kwargs):
        c = _SpyClient(**kwargs)
        sink.append(c)
        return c

    return _factory


def _make_sdk(options_sink, stateless_options, client_sink, settings=None):
    s = settings or _make_settings()
    with patch.object(ClaudeSDKBackend, "_initialize"):
        sdk = ClaudeAgentSDK(s)
    sdk._sdk_available = True
    sdk._cli_available = True
    sdk._ClaudeAgentOptions = _capturing_options(options_sink)
    sdk._ResultMessage = _FakeResultMsg
    sdk._SystemMessage = _FakeSystemMessage
    sdk._ClaudeSDKClient = _capturing_clients(client_sink)
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    sdk._AssistantMessage = None
    sdk._UserMessage = None
    sdk._ToolResultBlock = None

    async def _fake_query(prompt, options, init_session_id="sess-stateless-init"):
        stateless_options.append(options)
        yield _FakeSystemMessage(subtype="init", data={"session_id": init_session_id})
        yield _FakeResultMsg()

    sdk._query = _fake_query
    return sdk


def _patched(fn):
    """Run ``fn()`` under the LLM / router / MCP patches run() needs."""

    async def _inner():
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
                with patch.object(ClaudeSDKBackend, "_get_mcp_servers", return_value={}):
                    return await fn()

    return _inner


async def _drive_run(
    sdk,
    message,
    *,
    system_prompt="identity",
    history=None,
    session_handle=None,
    warm_client=None,
    on_client_built=None,
):
    async def _go():
        events = []
        async for ev in sdk.run(
            message,
            system_prompt=system_prompt,
            history=history,
            session_key="s1",
            session_handle=session_handle,
            warm_client=warm_client,
            on_client_built=on_client_built,
        ):
            events.append(ev)
        return events

    return await _patched(_go)()


# ===========================================================================
# 1. Warm reuse — key match drives the leased client directly
# ===========================================================================


async def test_warm_client_key_match_reuses_no_connect_no_resume_no_history() -> None:
    """A ``warm_client`` whose ``options_key`` MATCHES this turn drives the leased
    client's ``query`` directly: no ``connect``, no fresh client, no ``resume``,
    and the raw message (no injected history block) is sent."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    client_sink: list[_SpyClient] = []
    sdk = _make_sdk(options_sink, stateless_options, client_sink)

    # Phase 1 — supervised fresh build captures the live client + its options key
    # (mirrors the real supervisor binding the slot on turn 1).
    captured: dict = {}

    def _on_built(client, key, teardown):
        captured.update(client=client, key=key, teardown=teardown)

    await _drive_run(sdk, "turn one", on_client_built=_on_built)
    leased_client = captured["client"]
    leased_key = captured["key"]
    assert leased_client is client_sink[0]
    assert leased_client.connect_count == 1
    assert leased_client.queries == ["turn one"]

    # Phase 2 — lease it back with a MATCHING key + a history block. Warm reuse
    # must drive the SAME client with the raw message, never reconnect, never
    # build a second client, never inject the history into the query.
    lease = LeasedClient(client=leased_client, options_key=leased_key)
    events = await _drive_run(
        sdk,
        "turn two",
        history=[{"role": "user", "content": "an earlier turn"}],
        warm_client=lease,
    )

    assert any(e.type == "done" for e in events)
    assert len(client_sink) == 1, "warm reuse must NOT construct a second client"
    assert leased_client.connect_count == 1, "warm reuse must NOT reconnect the leased client"
    assert leased_client.queries == ["turn one", "turn two"], (
        "the second turn must drive the SAME leased client with the raw message"
    )
    assert leased_client.queries[-1] == "turn two", (
        "no history block may be injected into the query"
    )
    assert leased_client.disconnect_count == 0, "warm reuse must NEVER disconnect the leased client"
    assert not stateless_options, "a matching warm lease must not take the stateless path"
    assert lease.busy is False, "the busy flag must be released after the stream drains"


# ===========================================================================
# 2. Key mismatch — fresh build + on_client_built gets the NEW key
# ===========================================================================


async def test_warm_client_key_mismatch_builds_fresh_and_rebinds() -> None:
    """A ``warm_client`` whose key MISMATCHES + ``on_client_built`` builds a FRESH
    client and hands ``on_client_built`` the NEW key (not the stale one). The
    stale leased client is NOT driven."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    client_sink: list[_SpyClient] = []
    sdk = _make_sdk(options_sink, stateless_options, client_sink)

    stale = _SpyClient(init_session_id="stale")
    lease = LeasedClient(client=stale, options_key="STALE-MISMATCH-KEY")

    captured: dict = {}

    def _on_built(client, key, teardown):
        captured.update(client=client, key=key, teardown=teardown)

    events = await _drive_run(sdk, "turn two", warm_client=lease, on_client_built=_on_built)

    assert any(e.type == "done" for e in events)
    assert stale.queries == [], "the stale (mismatched) leased client must NOT be driven"
    assert captured, "on_client_built must be called to rebind the new slot"
    assert captured["client"] is client_sink[-1], "the bound client must be the freshly built one"
    assert captured["client"].connect_count == 1
    assert captured["client"].queries == ["turn two"]
    assert captured["key"] != "STALE-MISMATCH-KEY", (
        "on_client_built must receive THIS turn's key, not the stale lease key"
    )


# ===========================================================================
# 3. Supervised fresh build — callback invoked; resume only when cli_session_id
# ===========================================================================


async def test_supervised_fresh_build_invokes_callback_no_resume_on_turn_one() -> None:
    """No ``warm_client`` but ``on_client_built`` set → a fresh client is built,
    handed to the callback, and queried. With no resume id the options carry no
    ``resume`` (a true turn-1)."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    client_sink: list[_SpyClient] = []
    sdk = _make_sdk(options_sink, stateless_options, client_sink)

    captured: dict = {}

    def _on_built(client, key, teardown):
        captured.update(client=client, key=key, teardown=teardown)

    events = await _drive_run(sdk, "turn one", on_client_built=_on_built)

    assert any(e.type == "done" for e in events)
    assert captured["client"] is client_sink[-1]
    assert isinstance(captured["key"], str) and captured["key"]
    assert callable(captured["teardown"]), "teardown must be a callable the supervisor can await"
    assert captured["client"].queries == ["turn one"]
    assert getattr(options_sink[-1], "resume", None) is None, (
        "a turn-1 supervised build must not set resume on the options"
    )
    # teardown disconnects exactly the built client (the supervisor owns it).
    await captured["teardown"]()
    assert captured["client"].disconnect_count == 1


async def test_supervised_fresh_build_sets_resume_when_cli_session_id_present() -> None:
    """``on_client_built`` set AND ``session_handle.cli_session_id`` present → the
    fresh client's options carry ``resume=<id>`` (the cold-recovery path)."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    client_sink: list[_SpyClient] = []
    sdk = _make_sdk(options_sink, stateless_options, client_sink)

    captured: dict = {}

    def _on_built(client, key, teardown):
        captured.update(client=client, key=key, teardown=teardown)

    handle = SessionHandle(cli_session_id="sess-recover")
    events = await _drive_run(sdk, "resume turn", session_handle=handle, on_client_built=_on_built)

    assert any(e.type == "done" for e in events)
    assert captured, "on_client_built must be invoked on the supervised cold-recovery build"
    assert getattr(options_sink[-1], "resume", None) == "sess-recover", (
        "a supervised build with a cli_session_id must set resume on the options"
    )


# ===========================================================================
# 4. Legacy path — neither param → self._client warm path unchanged
# ===========================================================================


async def test_legacy_no_lease_params_uses_self_client_path() -> None:
    """With neither ``warm_client`` nor ``on_client_built`` the run takes the
    unchanged ``self._client`` warm path: a client is cached on ``self._client``
    and the stateless path is not used."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    client_sink: list[_SpyClient] = []
    sdk = _make_sdk(options_sink, stateless_options, client_sink)

    events = await _drive_run(sdk, "ordinary turn")

    assert any(e.type == "done" for e in events)
    assert sdk._client is not None, "the legacy warm path must cache the client on self._client"
    assert sdk._client is client_sink[0]
    assert sdk._client.queries == ["ordinary turn"]
    assert not stateless_options, "the legacy warm path must not fall back to stateless"


# ===========================================================================
# 5. Busy edge — a matching but busy lease falls back to stateless, no rebind
# ===========================================================================


async def test_busy_warm_client_falls_back_to_stateless_without_rebind() -> None:
    """A ``warm_client`` whose key matches but whose ``busy`` flag is already set
    (a sibling turn mid-query) falls back to a fresh stateless query for THIS turn
    and does NOT re-drive or rebind the leased client."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    client_sink: list[_SpyClient] = []
    sdk = _make_sdk(options_sink, stateless_options, client_sink)

    # Phase 1 — supervised build to capture a real matching key.
    captured: dict = {}

    def _on_built(client, key, teardown):
        captured.update(client=client, key=key, teardown=teardown)

    await _drive_run(sdk, "turn one", on_client_built=_on_built)
    leased_client = captured["client"]
    leased_key = captured["key"]

    # Phase 2 — lease it back as BUSY. The turn must take the stateless path and
    # leave the busy lease untouched.
    lease = LeasedClient(client=leased_client, options_key=leased_key, busy=True)
    events = await _drive_run(sdk, "turn two", warm_client=lease)

    assert any(e.type == "done" for e in events)
    assert stateless_options, "a busy matching lease must fall back to the stateless path"
    assert leased_client.queries == ["turn one"], (
        "the busy leased client must NOT be driven a second time"
    )
    assert len(client_sink) == 1, "the busy fallback must not build a new persistent client"
    assert lease.busy is True, "this run must not touch the busy flag the sibling turn owns"


# ===========================================================================
# 6. fix/warm-reuse — capture the native session_id from the ResultMessage
# ===========================================================================
#
# The leased supervised-fresh path was a live WARM NO-OP: SS-1 captured the
# native ``session_id`` ONLY from the init ``SystemMessage``'s
# ``data["session_id"]``, but on the fresh client that id never surfaced at
# runtime — so ``owns_capture`` stayed True forever and ``warm_reuse`` never
# fired. The fix ALSO captures ``session_id`` from the terminal ``ResultMessage``
# (a direct str field ALWAYS carried), gated on the handle + emit-once. These
# tests pin: (1) the ResultMessage fallback fires on the quirk stream (real
# reproduction — see the failing-pre-fix evidence in the PR/commit body), (2) the
# emit-once gate prevents a double-emit when BOTH sources carry the id, and (3)
# no handle → no session_id event from either source (legacy stream unchanged).

_RESULT_SID = "11111111-1111-4111-8111-111111111111"
_BOTH_SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class _FakeAssistantMsg:
    """A normal assistant text message — carries NO session_id."""

    def __init__(self, text: str = "hello from the model"):
        self.content = text


def _result_with_sid(sid: str) -> _FakeResultMsg:
    """A terminal ResultMessage carrying the native ``session_id`` as a direct str
    field (types.py: ``ResultMessage.session_id``) — the ALWAYS-present field the
    fix reads via ``getattr(event, "session_id", None)``."""
    r = _FakeResultMsg()
    r.session_id = sid
    return r


class _LeasedFreshSpyClient:
    """Mirrors the leased supervised-fresh RUNTIME QUIRK: the init ``SystemMessage``
    arrives but its ``data`` carries NO ``session_id`` (the id never surfaces on
    the fresh client), a normal assistant message follows, and only the terminal
    ``ResultMessage`` carries the native id. This is exactly the stream on which
    SS-1's SystemMessage-only capture was a NO-OP."""

    def __init__(self, options=None, *, result_session_id=_RESULT_SID, **_kw):
        self.options = options
        self._result_session_id = result_session_id
        self.queries: list[str] = []
        self.connect_count = 0
        self.disconnect_count = 0

    async def connect(self, prompt=None):
        self.connect_count += 1

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_messages(self):
        # init/system message WITH NO session_id — the runtime quirk.
        yield _FakeSystemMessage(subtype="init", data={})
        # a normal assistant message (carries no session_id).
        yield _FakeAssistantMsg("hello from the model")
        # terminal ResultMessage carrying the native session_id.
        yield _result_with_sid(self._result_session_id)

    async def disconnect(self):
        self.disconnect_count += 1

    async def interrupt(self):
        pass


class _BothSourcesSpyClient:
    """A stream where BOTH the init ``SystemMessage`` (``data.session_id``) AND the
    terminal ``ResultMessage`` (``session_id``) carry the SAME native id — proves
    the emit-once gate: the SystemMessage wins first and the ResultMessage capture
    is skipped, so exactly ONE ``session_id`` event is emitted."""

    def __init__(self, options=None, *, sid=_BOTH_SID, **_kw):
        self.options = options
        self._sid = sid
        self.queries: list[str] = []
        self.connect_count = 0
        self.disconnect_count = 0

    async def connect(self, prompt=None):
        self.connect_count += 1

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_messages(self):
        yield _FakeSystemMessage(subtype="init", data={"session_id": self._sid})
        yield _FakeAssistantMsg("hello from the model")
        yield _result_with_sid(self._sid)

    async def disconnect(self):
        self.disconnect_count += 1

    async def interrupt(self):
        pass


def _spy_factory(cls, sink: list):
    def _factory(**kwargs):
        c = cls(**kwargs)
        sink.append(c)
        return c

    return _factory


async def test_session_id_captured_from_result_message_on_leased_fresh_path() -> None:
    """REPRODUCTION — a leased supervised-fresh turn (``session_handle`` non-None,
    ``on_client_built`` set) whose stream carries NO session_id in the init
    ``SystemMessage`` but DOES carry it on the terminal ``ResultMessage`` must
    surface EXACTLY ONE ``session_id`` ``AgentEvent`` with that id. Pre-fix (no
    ResultMessage capture) this stream yields NO session_id event at all — the
    live WARM NO-OP."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    client_sink: list[_SpyClient] = []
    sdk = _make_sdk(options_sink, stateless_options, client_sink)
    sdk._AssistantMessage = _FakeAssistantMsg
    leased_sink: list[_LeasedFreshSpyClient] = []
    sdk._ClaudeSDKClient = _spy_factory(_LeasedFreshSpyClient, leased_sink)

    def _on_built(client, key, teardown):
        pass

    handle = SessionHandle()  # non-None, cli_session_id=None → turn-1 supervised fresh
    events = await _drive_run(sdk, "turn one", session_handle=handle, on_client_built=_on_built)

    sid_events = [e for e in events if e.type == "session_id"]
    assert len(sid_events) == 1, (
        "the ResultMessage fallback must surface exactly one session_id event; "
        f"got {[e.metadata for e in sid_events]}"
    )
    assert sid_events[0].metadata["session_id"] == _RESULT_SID, (
        "the captured id must be the one carried on the terminal ResultMessage"
    )
    # sanity: the leased-fresh spy really drove the supervised path for this turn.
    assert leased_sink and leased_sink[0].queries == ["turn one"]


async def test_no_double_emit_when_both_sources_carry_session_id() -> None:
    """NO-DOUBLE-EMIT — when BOTH the init ``SystemMessage`` and the terminal
    ``ResultMessage`` carry the id, EXACTLY ONE ``session_id`` event is emitted
    (from the SystemMessage; the ResultMessage capture is skipped by the emit-once
    gate)."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    client_sink: list[_SpyClient] = []
    sdk = _make_sdk(options_sink, stateless_options, client_sink)
    sdk._AssistantMessage = _FakeAssistantMsg
    both_sink: list[_BothSourcesSpyClient] = []
    sdk._ClaudeSDKClient = _spy_factory(_BothSourcesSpyClient, both_sink)

    def _on_built(client, key, teardown):
        pass

    events = await _drive_run(
        sdk, "turn one", session_handle=SessionHandle(), on_client_built=_on_built
    )

    sid_events = [e for e in events if e.type == "session_id"]
    assert len(sid_events) == 1, (
        "the emit-once gate must yield exactly one session_id event even when both "
        f"the SystemMessage and ResultMessage carry the id; got {len(sid_events)}"
    )
    assert sid_events[0].metadata["session_id"] == _BOTH_SID


async def test_no_handle_emits_no_session_id_from_either_source() -> None:
    """LEGACY — with ``session_handle=None`` neither the init ``SystemMessage`` nor
    the terminal ``ResultMessage`` may emit a ``session_id`` event, even when both
    carry the id (the legacy stream stays byte-identical)."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    client_sink: list[_SpyClient] = []
    sdk = _make_sdk(options_sink, stateless_options, client_sink)
    sdk._AssistantMessage = _FakeAssistantMsg
    both_sink: list[_BothSourcesSpyClient] = []
    sdk._ClaudeSDKClient = _spy_factory(_BothSourcesSpyClient, both_sink)

    def _on_built(client, key, teardown):
        pass

    events = await _drive_run(sdk, "turn one", session_handle=None, on_client_built=_on_built)

    sid_events = [e for e in events if e.type == "session_id"]
    assert sid_events == [], (
        "no session_handle → no session_id event may be emitted from either source"
    )
