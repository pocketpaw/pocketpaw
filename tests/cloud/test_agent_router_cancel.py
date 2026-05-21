"""Cancellation behavior for the cloud agent endpoint."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from pocketpaw_ee.cloud.chat import agent_router as mod


@pytest.mark.asyncio
async def test_stop_with_no_active_run_returns_404(cloud_app_client: AsyncClient):
    resp = await cloud_app_client.post("/cloud/chat/group/g1/agent/stop")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "active_run.not_found"


@pytest.mark.asyncio
async def test_second_request_cancels_first(cloud_app_client: AsyncClient):
    """A second POST for the same (scope, scope_id, user_id) triggers cancel
    on the first by setting its cancel event."""
    fake_ctx = SimpleNamespace(
        kind=SimpleNamespace(value="group"),
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=[],
    )

    async def fake_resolver(**kwargs):
        return fake_ctx

    async def fake_persist(ctx, body):
        return "user_msg_id"

    first_done = asyncio.Event()

    async def slow_stream(ctx, user_msg_id, body, cancel_event, *, history=None):
        yield (
            "stream_start",
            {"run_id": "r", "agent_id": "a1", "scope": "group", "scope_id": "g1"},
        )
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=5.0)
        finally:
            first_done.set()
        yield ("stream_end", {"assistant_message_id": None, "usage": {}, "cancelled": True})

    async def fast_stream(ctx, user_msg_id, body, cancel_event, *, history=None):
        yield ("stream_end", {"assistant_message_id": "m", "usage": {}, "cancelled": False})

    with (
        patch.object(mod, "resolve_scope_context", fake_resolver),
        patch.object(mod, "_persist_user_message", fake_persist),
    ):
        with patch.object(mod, "_run_agent_stream", slow_stream):
            first_task = asyncio.create_task(
                cloud_app_client.post("/cloud/chat/group/g1/agent", json={"content": "1"})
            )
            # Give the first request time to register its cancel_event
            await asyncio.sleep(0.05)
        with patch.object(mod, "_run_agent_stream", fast_stream):
            second = await cloud_app_client.post(
                "/cloud/chat/group/g1/agent", json={"content": "2"}
            )
        assert second.status_code == 200
        await asyncio.wait_for(first_task, timeout=2.0)
        assert first_done.is_set()
