"""SSE happy-path tests for the cloud agent chat endpoint.

Patches ``_run_agent_stream`` with an async generator yielding a scripted
event sequence so we can assert the wire format without needing a real
agent backend.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from pocketpaw_ee.cloud.chat import agent_router as mod


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Return [(event_name, data_dict), ...] from a streaming SSE body."""
    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        if not block.strip():
            continue
        name = None
        data_str = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_str = line[len("data: ") :]
        if name is not None:
            events.append((name, json.loads(data_str) if data_str else {}))
    return events


@pytest.mark.asyncio
async def test_sse_emits_persisted_then_stream_events(cloud_app_client: AsyncClient):
    fake_ctx = SimpleNamespace(
        kind=SimpleNamespace(value="group"),
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1", "u2"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=[],
    )

    async def fake_resolver(**kwargs):
        return fake_ctx

    async def fake_persist(ctx, body):
        return "user_msg_id_1"

    async def fake_run_stream(ctx, user_msg_id, body, cancel_event, *, history=None):
        yield (
            "stream_start",
            {"run_id": "r1", "agent_id": ctx.target_agent_id, "scope": "group", "scope_id": "g1"},
        )
        yield ("chunk", {"content": "hi ", "type": "text"})
        yield ("chunk", {"content": "there", "type": "text"})
        yield ("stream_end", {"assistant_message_id": "msg_2", "usage": {}, "cancelled": False})

    with (
        patch.object(mod, "resolve_scope_context", fake_resolver),
        patch.object(mod, "_persist_user_message", fake_persist),
        patch.object(mod, "_run_agent_stream", fake_run_stream),
    ):
        async with cloud_app_client.stream(
            "POST",
            "/cloud/chat/group/g1/agent",
            json={"content": "hello", "client_message_id": "c1"},
        ) as resp:
            assert resp.status_code == 200
            body = (await resp.aread()).decode()

    events = _parse_sse(body)
    names = [n for n, _ in events]
    assert names[0] == "message.persisted"
    assert events[0][1]["user_message_id"] == "user_msg_id_1"
    assert events[0][1]["client_message_id"] == "c1"
    assert names[1] == "stream_start"
    assert names.count("chunk") == 2
    assert names[-1] == "stream_end"
