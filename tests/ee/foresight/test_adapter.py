# tests/ee/foresight/test_adapter.py
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
from typing import Any

import pytest
from pocketpaw_ee.foresight.llm.adapter import (
    ClaudeCodeBackend,
    DeterministicFakeBackend,
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
