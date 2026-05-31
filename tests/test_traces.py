"""Tests for trace storage helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pocketpaw.traces import TraceStore


@pytest.mark.asyncio
async def test_trace_store_append_and_get_trace(tmp_path):
    store = TraceStore(root=tmp_path / "traces")
    trace = {
        "trace_id": "trace_1",
        "session_key": "cli:chat1",
        "started_at": "2026-04-20T10:00:00+00:00",
        "inbound": {"channel": "cli", "timestamp": "2026-04-20T10:00:00+00:00"},
        "total": {
            "status": "ok",
            "duration_ms": 123,
            "total_cost_usd": 0.015,
            "tool_count": 1,
            "llm_call_count": 1,
        },
    }

    await store.append_trace(trace)
    loaded = await store.get_trace("trace_1")

    assert loaded is not None
    assert loaded["trace_id"] == "trace_1"
    assert loaded["total"]["total_cost_usd"] == 0.015


@pytest.mark.asyncio
async def test_trace_store_list_filters(tmp_path):
    store = TraceStore(root=tmp_path / "traces")

    await store.append_trace(
        {
            "trace_id": "old_low",
            "session_key": "cli:chat1",
            "started_at": "2026-04-10T10:00:00+00:00",
            "inbound": {"channel": "cli"},
            "total": {
                "total_cost_usd": 0.01,
                "duration_ms": 10,
                "tool_count": 0,
                "llm_call_count": 1,
            },
        }
    )
    await store.append_trace(
        {
            "trace_id": "new_high",
            "session_key": "websocket:abc",
            "started_at": "2026-04-20T10:00:00+00:00",
            "inbound": {"channel": "websocket"},
            "total": {
                "total_cost_usd": 1.25,
                "duration_ms": 400,
                "tool_count": 2,
                "llm_call_count": 3,
            },
        }
    )

    traces = await store.list_traces(
        since="2026-04-19T00:00:00+00:00",
        limit=10,
        session_id="abc",
        min_cost=0.5,
    )

    assert [trace["trace_id"] for trace in traces] == ["new_high"]


@pytest.mark.asyncio
async def test_trace_store_retention_cleanup(tmp_path):
    store = TraceStore(root=tmp_path / "traces")
    store.root.mkdir(parents=True, exist_ok=True)

    old_date = (datetime.now(tz=UTC) - timedelta(days=10)).date().isoformat()
    new_date = datetime.now(tz=UTC).date().isoformat()

    (store.root / f"{old_date}.jsonl").write_text("{}\n", encoding="utf-8")
    (store.root / f"{new_date}.jsonl").write_text("{}\n", encoding="utf-8")

    removed = await store.cleanup_retention(3)

    assert removed == 1
    assert not (store.root / f"{old_date}.jsonl").exists()
    assert (store.root / f"{new_date}.jsonl").exists()
