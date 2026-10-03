# tests/test_claude_sdk_tracing.py
# Pins that a persistent-client turn on the Claude Agent SDK backend is traced by
# logfire's own ClaudeSDKClient instrumentation: one ``invoke_agent`` root span per
# turn, ``chat`` children carrying usage, and ``execute_tool <name>`` spans from the
# PreToolUse/PostToolUse hooks logfire injects. logfire opens all of these inside
# its patched ``receive_response``, so a backend that reads the stream any other way
# exports no gen_ai spans at all.
#
# The fake client subclasses the backend's real client class, so logfire's class
# patches (``__init__``, ``query``, ``receive_response``) apply. Only the transport
# is faked: raw CLI dicts go through the SDK's real ``parse_message``, and the fake
# CLI fires the injected tool hooks between tool_use and tool_result, as the real
# CLI's control protocol does.

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.claude_sdk import ClaudeAgentSDK
from pocketpaw.agents.model_router import ModelSelection, TaskComplexity

claude_agent_sdk = pytest.importorskip("claude_agent_sdk")

_MODEL = "claude-sonnet-4-5-20250929"


def _assistant(content: list, usage: dict | None = None) -> dict:
    return {"type": "assistant", "message": {"model": _MODEL, "content": content, "usage": usage}}


_TOOL_USE = _assistant(
    [{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/x"}}],
    {"input_tokens": 11, "output_tokens": 7},
)
_TOOL_RESULT = {
    "type": "user",
    "message": {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "boom", "is_error": True}
        ],
    },
}
_TEXT = _assistant([{"type": "text", "text": "Done."}], {"input_tokens": 20, "output_tokens": 3})
_RESULT = {
    "type": "result",
    "subtype": "success",
    "duration_ms": 10,
    "duration_api_ms": 8,
    "is_error": False,
    "num_turns": 2,
    "session_id": "sess-1",
    "total_cost_usd": 0.01,
    "usage": {"input_tokens": 31, "output_tokens": 10},
    "result": "Done.",
}
# A frame of a known type with a missing field: the SDK's parse_message raises
# MessageParseError for it, which kills its receive_messages() generator.
_MALFORMED = {"type": "assistant", "message": {}}


class _FakeQuery:
    """Stands in for the SDK's ``Query``: yields raw CLI frames and fires the
    tool hooks logfire injected into the client's options, the way the CLI does."""

    def __init__(self, client, frames: list):
        self._client = client
        self._frames = list(frames)

    async def _hook(self, event: str, payload: dict) -> None:
        for matcher in self._client.options.hooks.get(event, []):
            for hook in matcher.hooks:
                await hook(payload, "toolu_1", {"signal": None})

    async def receive_messages(self):
        while self._frames:
            frame = self._frames.pop(0)
            if frame is _TOOL_RESULT:
                await self._hook("PreToolUse", {"tool_name": "Read", "tool_input": {}})
                await self._hook("PostToolUseFailure", {"tool_name": "Read", "error": "boom"})
            yield frame

    async def close(self) -> None:
        pass


class _FakeTransport:
    async def write(self, data: str) -> None:
        pass


def _make_sdk(frames: list):
    settings = MagicMock()
    for k, v in {
        "agent_backend": "claude_agent_sdk",
        "tool_profile": "full",
        "tools_allow": [],
        "tools_deny": [],
        "smart_routing_enabled": False,
        "claude_sdk_provider": "anthropic",
        "claude_sdk_model": None,
        "claude_sdk_max_turns": None,
        "claude_sdk_cli_path": None,
        "sdk_load_bundled_skills": False,
        "anthropic_api_key": "sk-test-key",
    }.items():
        setattr(settings, k, v)
    sdk = ClaudeAgentSDK(settings)
    sdk._cli_available = True

    class _FakeClient(sdk._ClaudeSDKClient):
        async def connect(self, prompt=None):
            self._query = _FakeQuery(self, frames)
            self._transport = _FakeTransport()

        async def disconnect(self):
            pass

        async def interrupt(self):
            pass

    sdk._ClaudeSDKClient = _FakeClient
    return sdk


async def _drive(sdk) -> list:
    selection = ModelSelection(complexity=TaskComplexity.MODERATE, model=_MODEL, reason="test")
    with patch("pocketpaw.llm.client.resolve_llm_client") as resolve:
        llm = MagicMock()
        llm.is_ollama = llm.is_openai_compatible = llm.is_gemini = False
        llm.is_litellm = llm.is_openrouter = False
        llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        resolve.return_value = llm
        with patch("pocketpaw.agents.model_router.ModelRouter") as router:
            router.return_value.classify.return_value = selection
            with patch.object(type(sdk), "_get_mcp_servers", return_value={}):
                return [
                    ev
                    async for ev in sdk.run("read it", system_prompt="identity", session_key="s1")
                ]


@pytest.fixture
def instrumented(capfire):
    import logfire

    cls = claude_agent_sdk.ClaudeSDKClient
    assert not getattr(cls, "_is_instrumented_by_logfire", False), "leaked instrumentation"
    with logfire.instrument_claude_agent_sdk():
        yield capfire


def _spans(capfire) -> list[dict]:
    return capfire.exporter.exported_spans_as_dict()


async def test_persistent_turn_exports_invoke_agent_chat_and_tool_spans(instrumented):
    sdk = _make_sdk([_TOOL_USE, _TOOL_RESULT, _TEXT, _RESULT])

    events = await _drive(sdk)
    assert any(e.type == "message" and "Done." in (e.content or "") for e in events), events

    spans = _spans(instrumented)
    roots = [s for s in spans if s["name"] == "invoke_agent"]
    assert len(roots) == 1, [s["name"] for s in spans]
    root = roots[0]
    attrs = root["attributes"]
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.provider.name"] == "anthropic"
    assert attrs["gen_ai.response.model"] == _MODEL
    assert attrs["gen_ai.usage.input_tokens"] == 31
    assert attrs["gen_ai.usage.output_tokens"] == 10
    assert attrs["gen_ai.conversation.id"] == "sess-1"

    root_id = root["context"]["span_id"]
    chats = [s for s in spans if s["attributes"].get("gen_ai.operation.name") == "chat"]
    assert len(chats) >= 1
    assert all(c["parent"]["span_id"] == root_id for c in chats)
    # logfire marks per-message chat usage "partial"; the root carries the totals.
    assert any(c["attributes"].get("gen_ai.usage.partial.input_tokens") == 11 for c in chats)

    tools = [s for s in spans if s["name"] == "execute_tool Read"]
    assert len(tools) == 1, [s["name"] for s in spans]
    tool = tools[0]
    assert tool["parent"]["span_id"] == root_id
    assert tool["attributes"]["gen_ai.tool.name"] == "Read"
    assert tool["attributes"]["logfire.level_num"] >= 17  # error


async def test_parse_error_mid_turn_keeps_one_root_span_and_every_message(instrumented):
    sdk = _make_sdk([_TOOL_USE, _MALFORMED, _TOOL_RESULT, _TEXT, _RESULT])

    events = await _drive(sdk)
    assert any(e.type == "message" and "Done." in (e.content or "") for e in events), events

    roots = [s for s in _spans(instrumented) if s["name"] == "invoke_agent"]
    assert len(roots) == 1, "a recovered parse error must not split the turn"
    assert roots[0]["attributes"]["gen_ai.usage.output_tokens"] == 10


@pytest.mark.parametrize("cancel_after", [None, 5], ids=["completed", "cancelled"])
async def test_cloud_run_loop_detaches_the_turn_span_in_its_own_context(
    instrumented, caplog, monkeypatch, cancel_after
):
    # The cloud chat run loop (run_core._drive_agent_loop) steps the backend's
    # generator from a new task per event. logfire's invoke_agent span is attached
    # across ``yield``, so each step must run in the SAME Context or its detach
    # fails and logs "Failed to detach context" on every turn. A cancelled run
    # must close the half-read turn in that Context too, not leave it to GC.
    import asyncio
    import gc
    import logging
    from types import SimpleNamespace

    pytest.importorskip("pocketpaw_ee", reason="pocketpaw-ee not installed")
    from pocketpaw_ee.cloud.chat.agent_service import ScopeContext, ScopeKind
    from pocketpaw_ee.cloud.chat.runs import run_core

    sdk = _make_sdk([_TOOL_USE, _TOOL_RESULT, _TEXT, _RESULT])

    class _Pool:
        async def get(self, _agent_id):
            return SimpleNamespace(config={}, agent_name="A")

        def run(self, agent_id, content, session_key, **_kw):
            return sdk.run(content, system_prompt="identity", session_key=session_key)

    async def _empty(*a, **k):
        return ""

    checks = 0

    async def _is_cancelled():
        nonlocal checks
        checks += 1
        return cancel_after is not None and checks >= cancel_after

    monkeypatch.setattr(run_core, "get_agent_pool", lambda: _Pool())
    monkeypatch.setattr(run_core, "build_knowledge_context", _empty)
    monkeypatch.setattr(run_core, "build_behavior_instructions", lambda ctx, backend_name=None: "")
    monkeypatch.setattr(run_core, "attach_sse_event_sink", lambda q: None)
    monkeypatch.setattr(run_core, "attach_agent_identity", lambda **k: None)
    monkeypatch.setattr(run_core, "detach_sse_event_sink", lambda t: None)
    monkeypatch.setattr(run_core, "detach_agent_identity", lambda t: None)
    caplog.set_level(logging.ERROR, logger="opentelemetry.context")

    selection = ModelSelection(complexity=TaskComplexity.MODERATE, model=_MODEL, reason="test")
    ctx = ScopeContext(
        kind=ScopeKind.SESSION,
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
    )
    with patch("pocketpaw.llm.client.resolve_llm_client") as resolve:
        llm = MagicMock()
        llm.is_ollama = llm.is_openai_compatible = llm.is_gemini = False
        llm.is_litellm = llm.is_openrouter = False
        llm.to_sdk_env.return_value = {"ANTHROPIC_API_KEY": "sk-test"}
        resolve.return_value = llm
        with patch("pocketpaw.agents.model_router.ModelRouter") as router:
            router.return_value.classify.return_value = selection
            with patch.object(type(sdk), "_get_mcp_servers", return_value={}):
                frames = [
                    f
                    async for f in run_core._drive_agent_loop(
                        ctx,
                        user_content="read it",
                        attachments_in=None,
                        mentions_in=None,
                        history=None,
                        is_cancelled=_is_cancelled,
                        emit_stream_start=False,
                    )
                ]

    gc.collect()  # a generator left suspended is finalized here, from another Context
    await asyncio.sleep(0)
    detach = [r.getMessage() for r in caplog.records if "detach" in r.getMessage().lower()]
    assert not detach, detach
    spans = _spans(instrumented)
    roots = [s for s in spans if s["name"] == "invoke_agent"]
    assert len(roots) == 1, [s["name"] for s in spans]
    chats = [s for s in spans if s["attributes"].get("gen_ai.operation.name") == "chat"]
    assert chats and all(c["parent"]["span_id"] == roots[0]["context"]["span_id"] for c in chats)
    if cancel_after is None:
        assert any(n == "chunk" and "Done." in d["content"] for n, d in frames), frames
