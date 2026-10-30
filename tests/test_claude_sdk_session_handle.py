# tests/test_claude_sdk_session_handle.py
# Created: 2026-06-30 (feat/session-supervisor SS-1) — pins the wiring contract
# for native SDK ``resume`` on the OSS Claude SDK backend. The de-risk slice lets
# ONE agent hold a conversation across turns via the Claude Agent SDK's NATIVE
# ``resume`` (a fresh-process launch that reloads the on-disk session) instead of
# replaying Mongo history into the prompt, while a freshly-rebuilt system prompt
# is honored on every turn.
#
# Live ``claude`` model calls are BLOCKED in CI, so these tests SPY on the
# boundary (the constructed ``ClaudeAgentOptions`` + the faked SDK message stream)
# rather than making a real call. They assert four things:
#   1. ``run(session_handle=SessionHandle(cli_session_id="..."))`` constructs
#      options carrying ``resume="..."`` AND routes down the FRESH stateless
#      ``query()`` launch path (never the warm persistent client, which would
#      silently ignore a fresh ``resume``).
#   2. The per-turn ``system_prompt`` is reflected on the resume path — a second
#      call with a different system prompt builds options carrying THAT prompt
#      (per-turn injection survives the resume seam, not a frozen turn-1 prompt).
#   3. The turn-1 native ``session_id`` is extracted from the SDK init/system
#      message and surfaced once as a ``session_id`` AgentEvent.
#   4. The legacy path (no handle / cli_session_id=None) is UNCHANGED: no
#      ``resume`` is set and no ``session_id`` event is emitted.
#
# The spy targets ONLY the construction/stream boundary (the seam under test is
# claude_sdk's own resume-routing + capture logic, which is exercised for real).

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

from pocketpaw.agents.backend import SessionHandle
from pocketpaw.agents.claude_sdk import ClaudeAgentSDK, ClaudeSDKBackend
from pocketpaw.agents.model_router import ModelSelection, TaskComplexity

_LLM_CLIENT = "pocketpaw.llm.client.resolve_llm_client"
_MODEL_ROUTER = "pocketpaw.agents.model_router.ModelRouter"


# ===========================================================================
# Fakes — a capturing options factory + a faked SDK message stream, so a full
# run() completes WITHOUT a live model call and the constructed options + the
# emitted events are observable.
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
        # Mirror the real ClaudeAgentOptions default so a legacy run reads None.
        self.resume = kwargs.get("resume", None)


def _capturing_options(sink: list[_Options]):
    """Factory that records every constructed options object into ``sink``."""

    def _factory(**kwargs):
        opt = _Options(**kwargs)
        sink.append(opt)
        return opt

    return _factory


class _FakeSystemMessage:
    """Stand-in for the SDK's init/system message (carries ``data.session_id``)."""

    def __init__(self, subtype: str, data: dict):
        self.subtype = subtype
        self.data = data


class _FakeResultMsg:
    def __init__(self):
        self.is_error = False
        self.result = "ok"
        self.total_cost_usd = None
        self.usage = {}


class _FakeClient:
    """Warm persistent client. Its ``receive_messages()`` yields the init/system
    message (carrying ``session_id``) then a ResultMessage — the warm path the
    legacy + turn-1-capture runs take."""

    def __init__(self, options=None, *, init_session_id="sess-warm-init", **_kw):
        self.options = options
        self._init_session_id = init_session_id
        self.queries: list[str] = []

    async def connect(self, prompt=None):
        pass

    async def query(self, prompt, session_id="default"):
        self.queries.append(prompt)

    async def receive_messages(self):
        yield _FakeSystemMessage(subtype="init", data={"session_id": self._init_session_id})
        yield _FakeResultMsg()

    async def disconnect(self):
        pass

    async def interrupt(self):
        pass


def _wire_fakes(sdk, options_sink: list[_Options], stateless_options: list[_Options]):
    """Wire the SDK symbol table to the fakes. ``options_sink`` records EVERY
    constructed options; ``stateless_options`` records the options handed to the
    stateless ``query()`` launch path (proving a resume run took it)."""
    sdk._ClaudeAgentOptions = _capturing_options(options_sink)
    sdk._ResultMessage = _FakeResultMsg
    sdk._SystemMessage = _FakeSystemMessage
    sdk._ClaudeSDKClient = lambda **kwargs: _FakeClient(**kwargs)
    sdk._HookMatcher = MagicMock()
    sdk._StreamEvent = None
    sdk._AssistantMessage = None
    sdk._UserMessage = None
    sdk._ToolResultBlock = None

    async def _fake_query(prompt, options, init_session_id="sess-stateless-init"):
        # The stateless fresh-launch path — record the options it was handed and
        # emit the init/system message + result the SDK would.
        stateless_options.append(options)
        yield _FakeSystemMessage(subtype="init", data={"session_id": init_session_id})
        yield _FakeResultMsg()

    sdk._query = _fake_query


def _make_sdk(options_sink, stateless_options, settings=None):
    s = settings or _make_settings()
    with patch.object(ClaudeSDKBackend, "_initialize"):
        sdk = ClaudeAgentSDK(s)
    sdk._sdk_available = True
    sdk._cli_available = True
    _wire_fakes(sdk, options_sink, stateless_options)
    return sdk


def _patched(fn):
    """Run ``fn()`` under the standard LLM / router / MCP patches run() needs."""

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


async def _drive_run(sdk, message, *, system_prompt="identity", session_handle=None):
    async def _go():
        events = []
        async for ev in sdk.run(
            message,
            system_prompt=system_prompt,
            session_key="s1",
            session_handle=session_handle,
        ):
            events.append(ev)
        return events

    return await _patched(_go)()


# ===========================================================================
# 0. Signature contract
# ===========================================================================


def test_run_accepts_session_handle_kwarg() -> None:
    """``ClaudeSDKBackend.run`` accepts a keyword-only ``session_handle``
    defaulting to ``None`` (the legacy / non-supervised path)."""
    params = inspect.signature(ClaudeSDKBackend.run).parameters
    assert "session_handle" in params, "run must accept a session_handle kwarg"
    assert params["session_handle"].default is None, (
        "session_handle must default to None so non-supervised runs are unaffected"
    )


def test_session_handle_carries_store_opaquely() -> None:
    """SS-1 carries ``session_store`` as an inert pass-through field (SS-2 owns
    it); the dataclass exposes both fields with None defaults."""
    sentinel = object()
    sh = SessionHandle(cli_session_id="abc", session_store=sentinel)
    assert sh.cli_session_id == "abc"
    assert sh.session_store is sentinel
    assert SessionHandle().cli_session_id is None
    assert SessionHandle().session_store is None


# ===========================================================================
# 1. Resume wiring — options carry resume + the fresh-launch path is taken
# ===========================================================================


async def test_resume_sets_options_resume_and_uses_fresh_launch_path() -> None:
    """A run with ``SessionHandle(cli_session_id=...)`` constructs options
    carrying ``resume=<id>`` AND dispatches down the stateless ``query()``
    fresh-launch path (never the warm client, which would ignore the resume)."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    sdk = _make_sdk(options_sink, stateless_options)

    handle = SessionHandle(cli_session_id="sess-xyz")
    events = await _drive_run(
        sdk, "turn two", system_prompt="IDENTITY-ALPHA", session_handle=handle
    )

    assert any(e.type == "done" for e in events)
    assert options_sink, "options must have been constructed"
    opt = options_sink[-1]
    assert getattr(opt, "resume", None) == "sess-xyz", (
        "the constructed ClaudeAgentOptions must carry resume=<cli_session_id> so "
        "the CLI subprocess resumes that on-disk session natively"
    )
    # The SAME options object must reach the stateless launch path — proving the
    # warm persistent client (which freezes options at connect()) was bypassed.
    assert stateless_options and stateless_options[-1] is opt, (
        "a resume-bearing run must take the fresh stateless query() launch path"
    )


# ===========================================================================
# 2. Per-turn system prompt survives the resume seam
# ===========================================================================


async def test_per_turn_system_prompt_reflected_on_resume_path() -> None:
    """Resume must NOT freeze a turn-1 prompt: a second resume turn with a
    different system prompt builds options carrying THAT prompt, proving the
    freshly-rebuilt per-turn system prompt is honored every turn."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    sdk = _make_sdk(options_sink, stateless_options)
    handle = SessionHandle(cli_session_id="sess-1")

    await _drive_run(sdk, "turn A", system_prompt="IDENTITY-ALPHA", session_handle=handle)
    first = options_sink[-1]
    assert "IDENTITY-ALPHA" in first.system_prompt

    await _drive_run(sdk, "turn B", system_prompt="IDENTITY-BRAVO", session_handle=handle)
    second = options_sink[-1]
    assert "IDENTITY-BRAVO" in second.system_prompt, (
        "the second turn's options must reflect the NEW per-turn system prompt"
    )
    assert "IDENTITY-ALPHA" not in second.system_prompt, (
        "the per-turn prompt is rebuilt each turn — turn-1's prompt must not leak "
        "into turn-2's options (resume does not freeze the system prompt)"
    )


# ===========================================================================
# 3. Turn-1 native session-id capture surfaces on the event stream
# ===========================================================================


async def test_turn_one_session_id_is_captured_and_surfaced() -> None:
    """Turn 1 (handle present, cli_session_id=None) extracts the native
    ``session_id`` from the SDK init/system message and surfaces it once as a
    ``session_id`` AgentEvent so the controller can persist it (SS-3)."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    sdk = _make_sdk(options_sink, stateless_options)

    # Turn 1: no id yet → legacy warm path, but the handle opts into capture.
    handle = SessionHandle(cli_session_id=None)
    events = await _drive_run(sdk, "turn one", session_handle=handle)

    sid_events = [e for e in events if e.type == "session_id"]
    assert len(sid_events) == 1, "exactly one session_id event must be surfaced on turn 1"
    assert sid_events[0].metadata.get("session_id") == "sess-warm-init", (
        "the surfaced id must be the one carried in the init/system message data"
    )
    # Turn 1 has no cli_session_id, so no resume is set (capture-only).
    assert getattr(options_sink[-1], "resume", None) is None


# ===========================================================================
# 4. Legacy path unchanged — no resume, no session_id event
# ===========================================================================


async def test_legacy_path_no_handle_is_unchanged() -> None:
    """With NO session_handle, behavior is byte-identical to today: options carry
    no ``resume`` and the stream emits no ``session_id`` event."""
    options_sink: list[_Options] = []
    stateless_options: list[_Options] = []
    sdk = _make_sdk(options_sink, stateless_options)

    events = await _drive_run(sdk, "ordinary turn", session_handle=None)

    assert any(e.type == "done" for e in events)
    assert getattr(options_sink[-1], "resume", None) is None, (
        "a no-handle run must not set resume on the options"
    )
    assert not any(e.type == "session_id" for e in events), (
        "a no-handle run must not surface a session_id event (legacy stream is byte-identical)"
    )
    # And it must take the warm path, not the stateless fresh-launch path.
    assert not stateless_options, "a legacy run must not take the stateless resume path"
