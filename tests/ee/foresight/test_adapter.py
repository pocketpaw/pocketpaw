# tests/ee/foresight/test_adapter.py
# Updated: 2026-05-25 (feat/foresight-v02-oasis-camel-paw) — PR 2 adds:
#   - ClaudeCodeBackend.run(messages, ...) — CAMEL BaseModelBackend-shaped
#     surface returns OpenAI chat-completion dict.
#   - DeterministicFakeBackend.run(...) — same shape, deterministic.
#   - LiteLLMFallbackBackend stub — complete() + run() both raise
#     NotImplementedError with a PR 3 pointer.
#   - _compose_prompt + _role_tag flattening logic.
# Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 scaffold.
#
# Pin the v0.1 backend adapter contract:
#   - DeterministicFakeBackend cycles through verbs deterministically.
#   - DeterministicFakeBackend honors a scripted response list.
#   - ClaudeCodeBackend constructor validates max_concurrent.
#   - ClaudeCodeBackend uses an injected client_factory (no SDK
#     dependency in tests).
#   - ClaudeCodeBackend._await_terminal handles the three v0.1 response
#     shapes: bare string, async iterator of events, dict-shaped final.

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest
from pocketpaw_ee.foresight.llm.adapter import (
    ClaudeCodeBackend,
    DeterministicFakeBackend,
    LiteLLMFallbackBackend,
)

# --- DeterministicFakeBackend ---------------------------------------


async def test_fake_backend_default_cycles_through_verbs():
    backend = DeterministicFakeBackend()
    responses = [await backend.complete("ignored") for _ in range(5)]
    # First five verbs in the cycle
    assert "action=observe" in responses[0]
    assert "action=propose" in responses[1]
    assert "action=confirm" in responses[2]
    assert "action=amend" in responses[3]
    assert "action=approve" in responses[4]


async def test_fake_backend_default_includes_put_clause():
    backend = DeterministicFakeBackend()
    response = await backend.complete("ignored")
    assert "put=last_action:" in response


async def test_fake_backend_honors_scripted_responses():
    scripted = ["action=alpha; put=x:1", "action=beta; put=y:2"]
    backend = DeterministicFakeBackend(responses=scripted)
    a = await backend.complete("ignored")
    b = await backend.complete("ignored")
    c = await backend.complete("ignored")  # wraps around
    assert a == scripted[0]
    assert b == scripted[1]
    assert c == scripted[0]


async def test_fake_backend_call_count_tracks_invocations():
    backend = DeterministicFakeBackend()
    for _ in range(7):
        await backend.complete("ignored")
    assert backend.call_count == 7


# --- ClaudeCodeBackend ----------------------------------------------


def test_claude_backend_rejects_zero_concurrency():
    with pytest.raises(ValueError, match="max_concurrent"):
        ClaudeCodeBackend(max_concurrent=0)


def test_claude_backend_rejects_negative_concurrency():
    with pytest.raises(ValueError, match="max_concurrent"):
        ClaudeCodeBackend(max_concurrent=-1)


# --- ClaudeCodeBackend with injected factory ------------------------
#
# These tests exercise the adapter WITHOUT touching the real SDK.
# We hand in a client_factory that returns a fake context-manager-able
# client, and the adapter drives its `query` + `__aenter__` / `__aexit__`.


class _FakeSDKEvent:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAsyncIterator:
    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)


class _FakeSDKClient:
    """Mimics the bits of ClaudeSDKClient the adapter touches."""

    def __init__(self, response: Any) -> None:
        self._response = response
        self.entered = False
        self.exited = False
        self.queried_with: str | None = None

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True

    async def query(self, *, prompt: str):
        self.queried_with = prompt
        return self._response


async def test_claude_backend_returns_string_response_as_is():
    fake = _FakeSDKClient(response="hello world")
    backend = ClaudeCodeBackend(client_factory=lambda: fake)

    result = await backend.complete("test prompt")

    assert result == "hello world"
    assert fake.entered
    assert fake.exited
    assert fake.queried_with == "test prompt"


async def test_claude_backend_drains_async_iterator_of_events():
    events = [
        _FakeSDKEvent("partial"),
        _FakeSDKEvent("more partial"),
        _FakeSDKEvent("final answer"),
    ]
    fake = _FakeSDKClient(response=_FakeAsyncIterator(events))
    backend = ClaudeCodeBackend(client_factory=lambda: fake)

    result = await backend.complete("test prompt")

    # Latest event wins (SDK emits incremental + final)
    assert result == "final answer"


async def test_claude_backend_handles_dict_shaped_response():
    fake = _FakeSDKClient(response={"text": "from dict"})
    backend = ClaudeCodeBackend(client_factory=lambda: fake)

    result = await backend.complete("test prompt")

    assert result == "from dict"


async def test_claude_backend_handles_dict_with_content_key():
    fake = _FakeSDKClient(response={"content": "alt key"})
    backend = ClaudeCodeBackend(client_factory=lambda: fake)

    result = await backend.complete("test prompt")

    assert result == "alt key"


async def test_claude_backend_semaphore_serializes_burst():
    """The semaphore should cap concurrency. With max_concurrent=2 and
    a factory whose clients each sleep 0.05s, 6 concurrent calls take
    at least 3 batches × 0.05s = 0.15s.
    """

    class _SleepingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def query(self, *, prompt):  # noqa: ARG002
            await asyncio.sleep(0.05)
            return "ok"

    backend = ClaudeCodeBackend(client_factory=lambda: _SleepingClient(), max_concurrent=2)

    import time

    start = time.perf_counter()
    await asyncio.gather(*(backend.complete("p") for _ in range(6)))
    elapsed = time.perf_counter() - start

    # 6 calls / 2 concurrent = 3 batches × 0.05s = 0.15s lower bound
    assert elapsed >= 0.13, f"semaphore not serializing: {elapsed:.3f}s for 6 calls @ 2 concurrent"
    # And should be well under fully-serial (6 × 0.05 = 0.30s)
    assert elapsed < 0.28, f"semaphore over-serializing: {elapsed:.3f}s"


async def test_claude_backend_factory_can_be_async():
    """Factory returning a coroutine should be awaited automatically."""

    async def _async_factory():
        return _FakeSDKClient(response="async-built")

    backend = ClaudeCodeBackend(client_factory=_async_factory)
    result = await backend.complete("p")
    assert result == "async-built"


# --- PR 2: CAMEL BaseModelBackend-shaped surface --------------------
#
# These tests exercise the ``run(messages, response_format, tools)``
# surface PR 3 will pass to ``oasis.SocialAgent(model=...)``. The
# message objects are stub BaseMessage-shapes (we don't import
# camel.messages.BaseMessage here so tests stay runnable without
# pocketpaw-ee[foresight]).


@dataclass
class _FakeCAMELMessage:
    """Quacks like ``camel.messages.BaseMessage`` — has ``content`` and
    optionally ``role_name`` / ``role_type``. Used to test the adapter's
    prompt flattening without depending on CAMEL at test time.
    """

    content: str
    role_name: str = "User"
    role_type: str | None = None


async def test_claude_backend_run_returns_camel_chat_completion_shape():
    fake = _FakeSDKClient(response="here is my reply")
    backend = ClaudeCodeBackend(client_factory=lambda: fake)
    msgs = [_FakeCAMELMessage(content="hello world", role_name="User")]

    result = await backend.run(msgs)

    # OpenAI ChatCompletion-shaped dict so CAMEL's downstream parsers
    # (SocialAgent.perform_action_by_llm) consume it unchanged.
    assert isinstance(result, dict)
    assert result["object"] == "chat.completion"
    assert len(result["choices"]) == 1
    assert result["choices"][0]["message"]["role"] == "assistant"
    assert result["choices"][0]["message"]["content"] == "here is my reply"
    assert result["choices"][0]["message"]["tool_calls"] == []  # v0.2 stub
    assert result["choices"][0]["finish_reason"] == "stop"
    assert "usage" in result


async def test_claude_backend_run_flattens_message_list_into_prompt():
    """Multiple messages should flatten to a single newline-joined
    prompt the SDK sees (the SDK doesn't carry conversation history)."""
    fake = _FakeSDKClient(response="ok")
    backend = ClaudeCodeBackend(client_factory=lambda: fake)
    msgs = [
        _FakeCAMELMessage(content="be helpful", role_name="System"),
        _FakeCAMELMessage(content="hi", role_name="User"),
        _FakeCAMELMessage(content="hello there", role_name="Assistant"),
        _FakeCAMELMessage(content="what is 2+2?", role_name="User"),
    ]

    await backend.run(msgs)

    sent = fake.queried_with or ""
    # Each message body should appear in order
    assert "be helpful" in sent
    assert "hi" in sent
    assert "hello there" in sent
    assert "what is 2+2?" in sent
    assert sent.index("be helpful") < sent.index("hi") < sent.index("hello there")


async def test_claude_backend_run_accepts_empty_message_list():
    """An empty list should produce an empty prompt — not crash."""
    fake = _FakeSDKClient(response="empty-prompt-handled")
    backend = ClaudeCodeBackend(client_factory=lambda: fake)
    result = await backend.run([])
    assert result["choices"][0]["message"]["content"] == "empty-prompt-handled"


async def test_claude_backend_run_ignores_response_format_and_tools_at_v02():
    """v0.2 passes response_format / tools but doesn't act on them.
    PR 3 closes this gap; v0.2 just ensures the signature accepts them."""
    fake = _FakeSDKClient(response="ack")
    backend = ClaudeCodeBackend(client_factory=lambda: fake)
    msgs = [_FakeCAMELMessage(content="hi")]

    result = await backend.run(
        msgs,
        response_format={"type": "json_object"},
        tools=[{"type": "function", "function": {"name": "noop"}}],
    )

    assert result["choices"][0]["message"]["content"] == "ack"


def test_claude_backend_role_tag_extracts_system_messages():
    msg = _FakeCAMELMessage(content="be careful", role_name="System")
    assert ClaudeCodeBackend._role_tag(msg) == "SYSTEM"

    msg2 = _FakeCAMELMessage(content="hi", role_name="User")
    assert ClaudeCodeBackend._role_tag(msg2) == "USER"

    msg3 = _FakeCAMELMessage(content="answer", role_name="Assistant")
    assert ClaudeCodeBackend._role_tag(msg3) == "ASSISTANT"


def test_claude_backend_role_tag_handles_unknown_role_as_user():
    """Unrecognized roles default to USER — safe fallback."""
    msg = _FakeCAMELMessage(content="?", role_name="Bystander")
    assert ClaudeCodeBackend._role_tag(msg) == "USER"


def test_claude_backend_compose_prompt_handles_messages_without_content():
    """Messages with no ``content`` attr should fall through to ``str(msg)``
    without crashing."""
    backend = ClaudeCodeBackend()
    prompt = backend._compose_prompt(["just a string", "another"])
    assert "just a string" in prompt
    assert "another" in prompt


# --- PR 2: DeterministicFakeBackend.run ------------------------------


async def test_fake_backend_run_returns_camel_chat_completion_shape():
    backend = DeterministicFakeBackend()
    result = await backend.run([])

    assert isinstance(result, dict)
    assert result["object"] == "chat.completion"
    content = result["choices"][0]["message"]["content"]
    assert "action=" in content  # carries the deterministic action line


async def test_fake_backend_run_advances_call_count_like_complete():
    """Calling ``run`` should increment the same counter ``complete`` does,
    so a mixed-mode test still sees deterministic verb rotation.
    """
    backend = DeterministicFakeBackend()
    await backend.run([])  # advances counter via internal complete()
    await backend.complete("p")
    assert backend.call_count == 2


# --- PR 3: LiteLLMFallbackBackend is no longer a stub ----------------
#
# The deep contract — complete() / run() actually talk to LiteLLM —
# lives in tests/ee/foresight/test_litellm_fallback.py (which uses
# monkeypatch on ``litellm.acompletion`` so no network is needed).
# Here we only assert the constructor still works and the availability
# flag has flipped.


def test_litellm_backend_marked_available_at_v03():
    """PR 2 shipped False (stub); PR 3 flips the flag to True since the
    real ``litellm.acompletion`` proxy is now wired up.
    """
    assert LiteLLMFallbackBackend.BACKEND_AVAILABLE is True


def test_litellm_backend_constructor_accepts_kwargs_without_crashing():
    """The tier-pool builder iterates over fallback slots; the
    constructor must accept the full PR 3 kwarg surface.
    """
    backend = LiteLLMFallbackBackend(
        model="anthropic/claude-sonnet-4-7",
        base_url="https://api.example.com",
        api_key="sk-test",
        max_concurrent=64,
        extra_kwargs={"temperature": 0.7},
    )
    assert backend is not None
    assert backend._model == "anthropic/claude-sonnet-4-7"


def test_litellm_backend_rejects_zero_concurrency():
    with pytest.raises(ValueError, match="max_concurrent"):
        LiteLLMFallbackBackend(max_concurrent=0)
