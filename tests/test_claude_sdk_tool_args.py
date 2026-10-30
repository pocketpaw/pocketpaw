# tests/test_claude_sdk_tool_args.py
# Created: 2026-08-15 (HTN-4, feat/claude-sdk-tool-args) — pins the contract that a
# ``tool_use`` event from the DEFAULT agent backend carries the tool's REAL
# arguments.
#
# The bug this locks down: ``run``'s stream loop announces a tool from the partial
# ``content_block_start`` (name known, arguments not yet streamed, so ``input={}``)
# and again from the completed ``AssistantMessage`` (the SDK's fully assembled
# ``input``). An ``_announced_tools`` name guard suppressed the SECOND one — the
# only emission that ever had the arguments — so on a streamed turn every consumer
# saw ``input={}``. ``include_partial_messages`` is on whenever ``StreamEvent``
# imports, which is every ordinary run, so this was the normal case and not an edge.
#
# Live ``claude`` model calls are BLOCKED in CI, so these tests SPY on the boundary
# (a faked SDK message stream through the warm client) rather than making a real
# call — the seam under test is claude_sdk's own event-emission logic, which is
# exercised for real. The fakes and patch scaffolding mirror
# ``test_claude_sdk_session_handle.py``.

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pocketpaw.agents.claude_sdk import ClaudeAgentSDK, ClaudeSDKBackend
from pocketpaw.agents.model_router import ModelSelection, TaskComplexity

_LLM_CLIENT = "pocketpaw.llm.client.resolve_llm_client"
_MODEL_ROUTER = "pocketpaw.agents.model_router.ModelRouter"

# The arguments a consumer needs and used to be denied.
_WRITE_ARGS = {"file_path": "/tmp/notes.md", "content": "hello"}


# ===========================================================================
# Fakes — SDK message/block stand-ins. Each is a distinct real class so the
# loop's ``isinstance`` dispatch picks exactly one branch per event.
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
    """Real options stand-in so ``_client_cache_key`` / dispatch can read it."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)
        self.model = kwargs.get("model", "")
        self.allowed_tools = kwargs.get("allowed_tools", [])
        self.system_prompt = kwargs.get("system_prompt", "")
        self.plugins = kwargs.get("plugins", [])
        self.resume = kwargs.get("resume", None)


class _FakeStreamEvent:
    """Stand-in for the SDK's partial-message StreamEvent (``.event`` is the raw
    Anthropic streaming event dict)."""

    def __init__(self, event: dict):
        self.event = event


class _FakeToolUseBlock:
    """Stand-in for ``ToolUseBlock`` — carries the ASSEMBLED arguments."""

    # ``input`` shadows the builtin deliberately — it is the SDK's field name,
    # and ``_extract_tool_info`` reads it by that name off the real block.
    def __init__(self, name: str, input: dict):
        self.name = name
        self.input = input


class _FakeAssistantMessage:
    def __init__(self, content: list, model: str = "claude-sonnet-4-5-20250929"):
        self.content = content
        self.model = model


class _FakeResultMsg:
    def __init__(self):
        self.is_error = False
        self.result = "ok"
        self.total_cost_usd = None
        self.usage = {}


def _tool_block_start(name: str) -> _FakeStreamEvent:
    """The partial event that opens a tool block: name only, no arguments yet."""
    return _FakeStreamEvent(
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_01", "name": name, "input": {}},
        }
    )


def _text_delta(text: str) -> _FakeStreamEvent:
    return _FakeStreamEvent({"type": "content_block_delta", "delta": {"text": text}})


class _FakeClient:
    """Warm persistent client whose ``receive_messages()`` replays a scripted
    message list — the stream the real SDK would produce."""

    def __init__(self, script: list, options=None, **_kw):
        self._script = script
        self.options = options

    async def connect(self, prompt=None):
        pass

    async def query(self, prompt, session_id="default"):
        pass

    async def receive_messages(self):
        for msg in self._script:
            yield msg

    async def disconnect(self):
        pass

    async def interrupt(self):
        pass


def _make_sdk(script: list, *, streaming: bool):
    """Build an SDK wired to replay ``script``. ``streaming=False`` models a
    runtime whose SDK has no ``StreamEvent`` — no ``include_partial_messages``,
    so only the AssistantMessage branch ever runs."""
    with patch.object(ClaudeSDKBackend, "_initialize"):
        sdk = ClaudeAgentSDK(_make_settings())
    sdk._sdk_available = True
    sdk._cli_available = True

    sdk._ClaudeAgentOptions = lambda **kwargs: _Options(**kwargs)
    sdk._ClaudeSDKClient = lambda **kwargs: _FakeClient(list(script), **kwargs)
    sdk._HookMatcher = MagicMock()
    sdk._ResultMessage = _FakeResultMsg
    sdk._AssistantMessage = _FakeAssistantMessage
    sdk._ToolUseBlock = _FakeToolUseBlock
    sdk._StreamEvent = _FakeStreamEvent if streaming else None
    sdk._SystemMessage = None
    sdk._UserMessage = None
    sdk._ToolResultBlock = None
    sdk._TextBlock = None
    return sdk


async def _drive_run(sdk, message="do the thing"):
    """Run a full turn under the LLM / router / MCP patches ``run()`` needs."""

    async def _go():
        return [ev async for ev in sdk.run(message, system_prompt="identity", session_key="s1")]

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
                return await _go()


def _tool_events(events):
    return [e for e in events if e.type == "tool_use"]


# ===========================================================================
# 1. Streamed turn — the real arguments reach the consumer
# ===========================================================================


async def test_streamed_tool_call_surfaces_real_arguments() -> None:
    """On the streamed path the assembled ``input`` must reach consumers. This is
    the regression: the AssistantMessage emission carrying it was suppressed
    because the partial announcement had already claimed the tool's name."""
    sdk = _make_sdk(
        [
            _text_delta("Saving that for you."),
            _tool_block_start("Write"),
            _FakeAssistantMessage([_FakeToolUseBlock("Write", _WRITE_ARGS)]),
            _FakeResultMsg(),
        ],
        streaming=True,
    )

    tools = _tool_events(await _drive_run(sdk))

    assert any(t.metadata.get("input") == _WRITE_ARGS for t in tools), (
        "a streamed tool call must surface a tool_use event carrying the REAL "
        f"arguments — got {[t.metadata.get('input') for t in tools]}"
    )
    # Two events for one call is the intended shape: prompt announcement first,
    # resolved arguments second (consumers REPLACE the tool's status line).
    assert len(tools) == 2, f"expected a provisional + a resolved event, got {len(tools)}"
    provisional, resolved = tools
    assert provisional.metadata["name"] == "Write"
    assert provisional.metadata["input"] == {}
    assert provisional.metadata["input_pending"] is True, (
        "the announcement must flag that its empty input is a placeholder"
    )
    assert resolved.metadata["name"] == "Write"
    assert resolved.metadata["input"] == _WRITE_ARGS
    assert resolved.metadata["input_pending"] is False, (
        "the resolved emission must flag its input as final"
    )


async def test_streamed_announcement_still_precedes_the_model_reply() -> None:
    """The announcement's whole purpose is a PROMPT 'tool started' indicator, so
    it must still be emitted as the block opens — not deferred until the message
    completes."""
    sdk = _make_sdk(
        [
            _tool_block_start("Write"),
            _text_delta("done"),
            _FakeAssistantMessage([_FakeToolUseBlock("Write", _WRITE_ARGS)]),
            _FakeResultMsg(),
        ],
        streaming=True,
    )

    events = await _drive_run(sdk)
    types = [e.type for e in events]
    first_tool = types.index("tool_use")
    first_text = types.index("message")

    assert first_tool < first_text, (
        "the provisional announcement must be emitted when the tool block opens, "
        "ahead of the text streamed after it"
    )
    assert events[first_tool].metadata["input_pending"] is True


async def test_repeated_tool_name_resolves_each_call_separately() -> None:
    """Two calls to the SAME tool in one message must each surface their own
    arguments. The old guard was keyed on the tool NAME, so it collapsed repeats —
    the second call's arguments could never have been distinguished."""
    first_args = {"file_path": "/tmp/a.md"}
    second_args = {"file_path": "/tmp/b.md"}
    sdk = _make_sdk(
        [
            _tool_block_start("Read"),
            _tool_block_start("Read"),
            _FakeAssistantMessage(
                [
                    _FakeToolUseBlock("Read", first_args),
                    _FakeToolUseBlock("Read", second_args),
                ]
            ),
            _FakeResultMsg(),
        ],
        streaming=True,
    )

    resolved = [t for t in _tool_events(await _drive_run(sdk)) if not t.metadata["input_pending"]]

    assert [t.metadata["input"] for t in resolved] == [first_args, second_args], (
        "each tool call must resolve to its own arguments, not be deduplicated by name"
    )


# ===========================================================================
# 2. Non-streaming path — unchanged: exactly one event per call, with real args
# ===========================================================================


async def test_non_streaming_path_emits_exactly_one_tool_use_per_call() -> None:
    """Without ``StreamEvent`` the SDK gets no ``include_partial_messages``, so the
    AssistantMessage branch is the ONLY emitter. That path already carried real
    arguments and must not regress into zero or two events."""
    sdk = _make_sdk(
        [
            _FakeAssistantMessage([_FakeToolUseBlock("Write", _WRITE_ARGS)]),
            _FakeResultMsg(),
        ],
        streaming=False,
    )

    tools = _tool_events(await _drive_run(sdk))

    assert len(tools) == 1, f"the non-streaming path must emit ONE tool_use, got {len(tools)}"
    assert tools[0].metadata["name"] == "Write"
    assert tools[0].metadata["input"] == _WRITE_ARGS
    assert tools[0].metadata["input_pending"] is False


async def test_non_streaming_path_emits_one_event_per_call_for_repeats() -> None:
    """Two calls to the same tool, no streaming: two events, each with its own
    arguments — one per call, never collapsed and never duplicated."""
    first_args = {"file_path": "/tmp/a.md"}
    second_args = {"file_path": "/tmp/b.md"}
    sdk = _make_sdk(
        [
            _FakeAssistantMessage(
                [
                    _FakeToolUseBlock("Read", first_args),
                    _FakeToolUseBlock("Read", second_args),
                ]
            ),
            _FakeResultMsg(),
        ],
        streaming=False,
    )

    tools = _tool_events(await _drive_run(sdk))

    assert [t.metadata["input"] for t in tools] == [first_args, second_args]
    assert all(t.metadata["input_pending"] is False for t in tools)
