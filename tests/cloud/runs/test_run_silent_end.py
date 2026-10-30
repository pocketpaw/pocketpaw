# tests/cloud/runs/test_run_silent_end.py: an accepted chat run never ends in silence.
#
# A run that finishes with nothing to show (no text, no tool work, no artifact,
# no question) must end ``failed`` with an ``error`` frame the client renders,
# not ``completed`` with an empty ``stream_end``. And a reader that reattaches
# after the live stream expired gets that same error from the run doc, not a
# bare ``stream_end`` that reads as success.

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

from tests.cloud.runs.test_run_core import _noop, _spec, fake_resolve_scope_context

pytestmark = pytest.mark.asyncio


async def _nothing_to_show(spec, ctx):  # noqa: ARG001
    # What a torn-down or interrupted backend leaves behind: usage, maybe some
    # thinking, and no reply.
    yield ("thinking", {"content": "hmm"})
    yield ("token_usage", {"input_tokens": 10, "output_tokens": 0})


async def test_a_run_with_no_reply_ends_failed_with_a_visible_error(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    calls: list[dict[str, Any]] = []

    async def _terminal(run_id, *, status, error=None, **k):  # noqa: ARG001
        calls.append({"fn": "mark_terminal", "status": status, "error": error})

    async def _completed(run_id, **k):  # noqa: ARG001
        calls.append({"fn": "mark_completed"})

    monkeypatch.setattr(run_core, "_iter_agent_events", _nothing_to_show)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr(run_core.run_service, "mark_terminal", _terminal)
    monkeypatch.setattr(run_core.run_service, "mark_completed", _completed)

    await run_core.execute_run(_spec())

    assert len(calls) == 1 and calls[0]["fn"] == "mark_terminal", calls
    assert calls[0]["status"] == "failed"
    assert calls[0]["error"]
    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert events[-1].event == "error", [e.event for e in events]
    assert events[-1].data["code"] == run_core.EMPTY_REPLY_CODE
    assert events[-1].data["message"]
    assert "stream_end" not in [e.event for e in events]


async def test_a_failed_run_whose_stream_expired_replays_its_error(
    runs_app_client,
    seed_run,  # noqa: ARG001
    monkeypatch,
):
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    await run_service.mark_terminal("r1", status="failed", error="The agent could not answer.")
    transport = RedisStreamTransport(fakeredis.aioredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.router.get_stream_transport", lambda: transport
    )

    resp = await runs_app_client.get("/cloud/chat/runs/r1/stream?after=0")

    assert "event: error" in resp.text, resp.text
    assert "The agent could not answer." in resp.text
    assert "event: stream_end" not in resp.text
