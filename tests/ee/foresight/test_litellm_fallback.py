# tests/ee/foresight/test_litellm_fallback.py
# Created: 2026-05-25 (feat/foresight-v03-calibration) — RFC 08 PR 3.
#
# Pin the LiteLLMFallbackBackend contract (RFC §6.4):
#   - BACKEND_AVAILABLE is True at v0.3 (PR 3 wires the real proxy).
#   - Constructor rejects zero / negative concurrency.
#   - complete(prompt) proxies through litellm.acompletion.
#   - run(messages, ...) builds an OpenAI-shaped messages list,
#     forwards tools + response_format.
#   - _response_to_camel normalizes provider response shapes to dict.
#   - Semaphore caps concurrency.
#
# Tests use monkeypatched ``litellm.acompletion`` so they run without
# network or API keys.

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest
from pocketpaw_ee.foresight.llm.adapter import LiteLLMFallbackBackend

# --- Fake response shapes --------------------------------------------


@dataclass
class _FakeMessage:
    role: str
    content: str
    tool_calls: list | None = None


@dataclass
class _FakeChoice:
    message: _FakeMessage
    index: int = 0
    finish_reason: str = "stop"


@dataclass
class _FakeUsage:
    prompt_tokens: int = 10
    completion_tokens: int = 5
    total_tokens: int = 15


@dataclass
class _FakeLiteLLMResponse:
    id: str = "test-response"
    choices: list = None
    usage: _FakeUsage = None

    def __post_init__(self):
        if self.choices is None:
            self.choices = [_FakeChoice(_FakeMessage(role="assistant", content="ok"))]
        if self.usage is None:
            self.usage = _FakeUsage()


# --- Surface-level tests ---------------------------------------------


def test_backend_available_flag_is_true_at_pr3():
    """PR 2 shipped False (stub); PR 3 flips to True."""
    assert LiteLLMFallbackBackend.BACKEND_AVAILABLE is True


def test_constructor_rejects_zero_concurrency():
    with pytest.raises(ValueError, match="max_concurrent"):
        LiteLLMFallbackBackend(max_concurrent=0)


def test_constructor_rejects_negative_concurrency():
    with pytest.raises(ValueError, match="max_concurrent"):
        LiteLLMFallbackBackend(max_concurrent=-1)


def test_constructor_accepts_optional_kwargs():
    backend = LiteLLMFallbackBackend(
        model="anthropic/claude-haiku-4-7",
        base_url="https://api.example.com",
        api_key="sk-test-123",
        max_concurrent=64,
        extra_kwargs={"temperature": 0.5},
    )
    assert backend._model == "anthropic/claude-haiku-4-7"
    assert backend._base_url == "https://api.example.com"
    assert backend._api_key == "sk-test-123"
    assert backend._extra_kwargs == {"temperature": 0.5}


# --- complete(prompt) ------------------------------------------------


async def test_complete_calls_litellm_acompletion(monkeypatch):
    """complete() should call litellm.acompletion with the right shape."""
    captured: dict = {}

    async def _fake_acompletion(**kwargs):
        captured.update(kwargs)
        return _FakeLiteLLMResponse(
            choices=[_FakeChoice(_FakeMessage(role="assistant", content="hello"))]
        )

    monkeypatch.setattr("litellm.acompletion", _fake_acompletion)

    backend = LiteLLMFallbackBackend(model="anthropic/claude-haiku-4-7")
    result = await backend.complete("test prompt")

    assert result == "hello"
    assert captured["model"] == "anthropic/claude-haiku-4-7"
    assert captured["messages"] == [{"role": "user", "content": "test prompt"}]


async def test_complete_falls_back_to_default_tail_model(monkeypatch):
    """No model in constructor → DEFAULT_TAIL_MODEL."""
    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _FakeLiteLLMResponse()

    monkeypatch.setattr("litellm.acompletion", _fake)

    backend = LiteLLMFallbackBackend()
    await backend.complete("x")
    assert captured["model"] == LiteLLMFallbackBackend.DEFAULT_TAIL_MODEL


async def test_complete_forwards_base_url_and_api_key(monkeypatch):
    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _FakeLiteLLMResponse()

    monkeypatch.setattr("litellm.acompletion", _fake)

    backend = LiteLLMFallbackBackend(
        model="custom-model",
        base_url="http://localhost:8000",
        api_key="key-xyz",
    )
    await backend.complete("x")
    assert captured["base_url"] == "http://localhost:8000"
    assert captured["api_key"] == "key-xyz"


async def test_complete_forwards_extra_kwargs(monkeypatch):
    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _FakeLiteLLMResponse()

    monkeypatch.setattr("litellm.acompletion", _fake)

    backend = LiteLLMFallbackBackend(
        model="custom-model",
        extra_kwargs={"temperature": 0.0, "max_tokens": 100},
    )
    await backend.complete("x")
    assert captured["temperature"] == 0.0
    assert captured["max_tokens"] == 100


async def test_complete_handles_dict_shaped_response(monkeypatch):
    """LiteLLM sometimes returns plain dicts (older versions)."""

    async def _fake(**kwargs):  # noqa: ARG001
        return {
            "choices": [{"message": {"role": "assistant", "content": "from dict"}}],
            "usage": {},
        }

    monkeypatch.setattr("litellm.acompletion", _fake)

    backend = LiteLLMFallbackBackend(model="x")
    result = await backend.complete("y")
    assert result == "from dict"


# --- run(messages, ...) ---------------------------------------------


@dataclass
class _FakeMsg:
    content: str
    role_name: str = "User"


async def test_run_builds_openai_messages_from_camel_shape(monkeypatch):
    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _FakeLiteLLMResponse(
            choices=[_FakeChoice(_FakeMessage(role="assistant", content="ok"))]
        )

    monkeypatch.setattr("litellm.acompletion", _fake)

    backend = LiteLLMFallbackBackend(model="anthropic/claude-haiku-4-7")
    msgs = [
        _FakeMsg(content="be helpful", role_name="System"),
        _FakeMsg(content="hi", role_name="User"),
        _FakeMsg(content="hello there", role_name="Assistant"),
        _FakeMsg(content="what is 2+2?", role_name="User"),
    ]
    result = await backend.run(msgs)

    assert isinstance(result, dict)
    assert "choices" in result

    sent_msgs = captured["messages"]
    assert sent_msgs[0] == {"role": "system", "content": "be helpful"}
    assert sent_msgs[1] == {"role": "user", "content": "hi"}
    assert sent_msgs[2] == {"role": "assistant", "content": "hello there"}
    assert sent_msgs[3] == {"role": "user", "content": "what is 2+2?"}


async def test_run_forwards_response_format(monkeypatch):
    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _FakeLiteLLMResponse()

    monkeypatch.setattr("litellm.acompletion", _fake)

    backend = LiteLLMFallbackBackend(model="x")
    await backend.run([_FakeMsg(content="hi")], response_format={"type": "json_object"})
    assert captured["response_format"] == {"type": "json_object"}


async def test_run_forwards_openai_tools_unchanged(monkeypatch):
    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _FakeLiteLLMResponse()

    monkeypatch.setattr("litellm.acompletion", _fake)

    tool_schema = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the weather",
            "parameters": {"type": "object", "properties": {}},
        },
    }

    backend = LiteLLMFallbackBackend(model="x")
    await backend.run([_FakeMsg(content="hi")], tools=[tool_schema])
    assert captured["tools"] == [tool_schema]


async def test_run_unwraps_camel_function_tool(monkeypatch):
    """CAMEL FunctionTool with openai_tool_schema attr should unwrap."""
    captured: dict = {}

    async def _fake(**kwargs):
        captured.update(kwargs)
        return _FakeLiteLLMResponse()

    monkeypatch.setattr("litellm.acompletion", _fake)

    class _FakeFunctionTool:
        openai_tool_schema = {
            "type": "function",
            "function": {"name": "my_tool", "description": "", "parameters": {}},
        }

    backend = LiteLLMFallbackBackend(model="x")
    await backend.run([_FakeMsg(content="hi")], tools=[_FakeFunctionTool()])
    assert captured["tools"][0]["function"]["name"] == "my_tool"


async def test_run_returns_camel_chat_completion_shape(monkeypatch):
    async def _fake(**kwargs):  # noqa: ARG001
        return _FakeLiteLLMResponse(
            choices=[_FakeChoice(_FakeMessage(role="assistant", content="answer"))]
        )

    monkeypatch.setattr("litellm.acompletion", _fake)

    backend = LiteLLMFallbackBackend(model="x")
    result = await backend.run([_FakeMsg(content="q")])
    assert isinstance(result, dict)
    assert result["choices"][0]["message"]["content"] == "answer"


# --- Semaphore ------------------------------------------------------


async def test_semaphore_caps_concurrent_calls(monkeypatch):
    """6 calls @ max_concurrent=2 → at least 3 batches of 0.05s wall-clock."""
    import time

    async def _slow(**kwargs):  # noqa: ARG001
        await asyncio.sleep(0.05)
        return _FakeLiteLLMResponse()

    monkeypatch.setattr("litellm.acompletion", _slow)

    backend = LiteLLMFallbackBackend(model="x", max_concurrent=2)
    start = time.perf_counter()
    await asyncio.gather(*(backend.complete("p") for _ in range(6)))
    elapsed = time.perf_counter() - start

    assert elapsed >= 0.13, f"semaphore not serializing: {elapsed:.3f}s"
    assert elapsed < 0.30, f"semaphore over-serializing: {elapsed:.3f}s"
