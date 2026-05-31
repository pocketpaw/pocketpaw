"""Tests for TraceCollector event aggregation."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from pocketpaw.bus.events import SystemEvent
from pocketpaw.bus.queue import MessageBus
from pocketpaw.trace_collector import TraceCollector
from pocketpaw.traces import TraceStore


@pytest.mark.asyncio
async def test_trace_collector_subscribe_and_unsubscribe(tmp_path):
    bus = MessageBus()
    collector = TraceCollector(store=TraceStore(root=tmp_path / "traces"))

    with patch("pocketpaw.bus.get_message_bus", return_value=bus):
        await collector.subscribe()
        assert collector.snapshot()["subscribed"] is True
        assert collector._on_event in bus._system_subscribers

        await collector.unsubscribe()
        assert collector.snapshot()["subscribed"] is False
        assert collector._on_event not in bus._system_subscribers


@pytest.mark.asyncio
async def test_trace_collector_aggregates_and_persists_trace(tmp_path):
    store = TraceStore(root=tmp_path / "traces")
    collector = TraceCollector(store=store)

    trace_id = "trace_agg_1"
    session_key = "cli:chat1"

    await collector._on_event(
        SystemEvent(
            event_type="trace_start",
            data={
                "trace_id": trace_id,
                "session_key": session_key,
                "started_at": "2026-04-20T10:00:00+00:00",
                "inbound": {
                    "channel": "cli",
                    "chat_id": "chat1",
                    "sender_id": "user1",
                    "timestamp": "2026-04-20T10:00:00+00:00",
                },
            },
        )
    )

    await collector._on_event(
        SystemEvent(
            event_type="agent_start",
            data={
                "trace_id": trace_id,
                "session_key": session_key,
                "backend": "claude_agent_sdk",
            },
        )
    )
    await collector._on_event(
        SystemEvent(
            event_type="tool_start",
            data={
                "trace_id": trace_id,
                "session_key": session_key,
                "name": "bash",
                "params": {"command": "echo hi"},
                "tool_call_id": "call_1",
            },
        )
    )
    await collector._on_event(
        SystemEvent(
            event_type="tool_result",
            data={
                "trace_id": trace_id,
                "session_key": session_key,
                "name": "bash",
                "result": "ok",
                "status": "success",
                "tool_call_id": "call_1",
            },
        )
    )
    await collector._on_event(
        SystemEvent(
            event_type="token_usage",
            data={
                "trace_id": trace_id,
                "session_key": session_key,
                "backend": "claude_agent_sdk",
                "model": "claude-3-haiku",
                "input_tokens": 100,
                "output_tokens": 50,
                "cached_input_tokens": 10,
                "total_cost_usd": 0.0123,
            },
        )
    )
    await collector._on_event(
        SystemEvent(
            event_type="trace_end",
            data={
                "trace_id": trace_id,
                "session_key": session_key,
                "status": "ok",
                "reason": "completed",
                "outbound": {
                    "channel": "cli",
                    "timestamp": "2026-04-20T10:00:05+00:00",
                    "chunks_count": 2,
                },
            },
        )
    )

    trace = await store.get_trace(trace_id)
    assert trace is not None
    assert trace["trace_id"] == trace_id
    assert trace["session_key"] == session_key
    assert trace["total"]["llm_call_count"] == 1
    assert trace["total"]["tool_count"] == 1
    assert trace["outbound"]["chunks_count"] == 2
    assert trace["llm_calls"][0]["input_tokens"] == 100
