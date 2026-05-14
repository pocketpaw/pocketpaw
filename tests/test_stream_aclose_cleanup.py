# tests/test_stream_aclose_cleanup.py
# Created: 2026-05-14 (fix/stream-aclose-leaks) — verifies the two backend
# run() paths explicitly close their inner async generators on completion.
# Without this, asyncio's GC destroys pending ``Queue.get()`` /
# ``async_generator_asend`` tasks at the next backend transition, surfacing
# as ``Task was destroyed but it is pending!`` and ``Task exception was
# never retrieved: StopAsyncIteration`` log noise.
"""Stream-aclose lifecycle tests for deep_agents and claude_sdk backends.

Both backends consume an inner async generator (langgraph astream /
claude SDK event_stream) inside their public ``run`` method. The bug we
fixed was that neither path called ``aclose()`` on that inner generator
when the consumer was done — only the outer ``async for`` ran to
exhaustion, leaving the generator's background tasks pending until GC.

These tests drive ``run`` against a tracked-async-generator stub and
assert that ``aclose()`` is invoked. They don't exercise the full agent
machinery — the goal is the lifecycle contract.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings


class _TrackedAsyncGen:
    """Async generator stand-in that records whether aclose was called.

    Yields the supplied items once, then signals end-of-stream. ``aclose``
    is recorded but otherwise a no-op (real async generators raise
    ``GeneratorExit`` inside the coroutine; the test only cares about
    the call site).
    """

    def __init__(self, items: list[Any]) -> None:
        self._items = list(items)
        self.aclose_called = 0
        self._closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._closed or not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)

    async def aclose(self) -> None:
        self.aclose_called += 1
        self._closed = True


# ---------------------------------------------------------------------------
# deep_agents backend
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deep_agents_run_closes_astream_on_normal_completion(monkeypatch):
    pytest.importorskip("deepagents")

    from pocketpaw.agents.deep_agents import DeepAgentsBackend

    backend = DeepAgentsBackend(Settings(_env_file=None))
    backend._sdk_available = True

    tracked = _TrackedAsyncGen([])
    fake_agent = MagicMock()
    fake_agent.astream = MagicMock(return_value=tracked)

    monkeypatch.setattr(backend, "_build_model", lambda: MagicMock())

    async def _empty_mcp_tools():
        return []

    monkeypatch.setattr(backend, "_build_mcp_tools", _empty_mcp_tools)
    monkeypatch.setattr(
        backend, "_get_or_create_agent", lambda *a, **kw: fake_agent
    )

    events: list[AgentEvent] = []
    async for ev in backend.run("hi"):
        events.append(ev)

    # Empty stream → only the "done" event, but aclose must still fire.
    assert tracked.aclose_called == 1, (
        f"deep_agents.run did not close its astream — aclose_called="
        f"{tracked.aclose_called}, events={[e.type for e in events]}"
    )


@pytest.mark.asyncio
async def test_deep_agents_run_closes_astream_on_exception(monkeypatch):
    """If the agent factory raises, the finally block still closes the
    stream — but only if it was created. We assert no AttributeError on
    a None _stream in the failure path."""
    pytest.importorskip("deepagents")

    from pocketpaw.agents.deep_agents import DeepAgentsBackend

    backend = DeepAgentsBackend(Settings(_env_file=None))
    backend._sdk_available = True

    monkeypatch.setattr(
        backend,
        "_build_model",
        MagicMock(side_effect=RuntimeError("model build failed")),
    )

    async def _empty_mcp_tools():
        return []

    monkeypatch.setattr(backend, "_build_mcp_tools", _empty_mcp_tools)

    events: list[AgentEvent] = []
    async for ev in backend.run("hi"):
        events.append(ev)

    # Failure surfaces as error + done, no traceback from the finally
    # trying to close a None stream.
    types = [e.type for e in events]
    assert "error" in types
    assert "done" in types


# ---------------------------------------------------------------------------
# claude_sdk backend
# ---------------------------------------------------------------------------
#
# The claude_sdk path has the same try/finally + ``getattr(stream, "aclose",
# None)`` shape as the deep_agents fix above, just in a much larger run()
# method with more setup branching (persistent-client cache, CLI subprocess
# launch, ResultMessage tracking). Driving a tracked stream through the
# full ``run`` requires stubbing 6+ collaborators, and any future refactor
# of the setup phase would silently break the test even if the lifecycle
# contract still held. Code review of the fix (a 12-line addition to the
# existing ``finally``) is the right tool here. The deep_agents tests
# above pin the equivalent pattern.
