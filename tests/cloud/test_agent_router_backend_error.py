# 2026-05-23 — Regression cover for the agent-router event loop:
# a backend that yields ``AgentEvent(type="error", ...)`` must surface
# its message to the client as an SSE ``error`` frame, not be silently
# swallowed. The original loop only handled message / thinking /
# tool_use / tool_result / done, which meant a misconfigured backend
# (e.g. codex_cli without ``openai-codex-sdk`` installed) caused the
# stream to end with ``assistant_message_id: null`` and no diagnostic
# reaching the UI.

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from pocketpaw_ee.cloud.chat import agent_router as mod


class _ErrorYieldingPool:
    """Mimics AgentPool with a backend that yields a single error event."""

    def __init__(self, message: str = "openai-codex-sdk is not installed"):
        self.message = message
        self.observed: list[tuple[str, str, str]] = []

    async def get(self, agent_id):
        return SimpleNamespace(
            agent_id=agent_id,
            agent_name="Agent " + agent_id,
            config={"backend": "codex_cli"},
        )

    async def run(
        self,
        agent_id,
        message,
        session_key,
        history=None,
        knowledge_context="",
        instructions="",
    ):
        # Mirrors the real codex_cli / claude_sdk early-return path
        # when an optional dependency is missing.
        yield SimpleNamespace(type="error", content=self.message, metadata={})

    async def observe(self, agent_id, user_input, agent_output):
        self.observed.append((agent_id, user_input, agent_output))


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        name = None
        data = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if name:
            out.append((name, json.loads(data) if data else {}))
    return out


@pytest.mark.asyncio
async def test_backend_error_event_surfaces_as_sse_error(cloud_app_client: AsyncClient):
    fake_ctx = SimpleNamespace(
        kind=SimpleNamespace(value="group"),
        scope_id="g1",
        workspace_id="w1",
        user_id="u1",
        members=["u1", "u2"],
        target_agent_id="agent_X",
        agent_ids_in_scope=["agent_X"],
        pocket_tool_specs=[],
        session_id=None,
        pocket_id=None,
        intent=None,
    )
    fake_pool = _ErrorYieldingPool(message="openai-codex-sdk is not installed: ...")

    async def fake_resolver(**kwargs):
        return fake_ctx

    async def fake_persist(ctx, body):
        return "user_msg_id_1"

    typing_calls: list[tuple[object, bool]] = []

    async def fake_broadcast_typing(ctx, active):
        typing_calls.append((ctx, active))
        return None

    async def fake_build_knowledge_context(_ctx, *, user_message, attachments=None, mentions=None):
        return ""

    with (
        patch.object(mod, "resolve_scope_context", fake_resolver),
        patch.object(mod, "_persist_user_message", fake_persist),
        patch.object(mod, "_broadcast_agent_typing", fake_broadcast_typing),
        patch.object(mod, "build_knowledge_context", fake_build_knowledge_context),
        patch.object(mod, "get_agent_pool", lambda: fake_pool),
    ):
        async with cloud_app_client.stream(
            "POST",
            "/cloud/chat/group/g1/agent",
            json={"content": "hi"},
        ) as resp:
            assert resp.status_code == 200
            body = (await resp.aread()).decode()

    events = _parse_sse(body)
    names = [n for n, _ in events]

    # The error must appear as a dedicated SSE frame, terminating the stream.
    # The outer ``gen()`` wrapper in ``post_agent_chat`` breaks on either
    # ``stream_end`` or ``error``, so ``error`` here is the legitimate
    # terminator — what we're guarding against is the prior silent-drop
    # behavior where no error frame fired at all.
    assert "error" in names, (
        f"backend error was swallowed — expected an `error` SSE frame, got: {names}"
    )
    error_payload = next(p for n, p in events if n == "error")
    assert error_payload["code"] == "agent.backend_error"
    assert "openai-codex-sdk" in error_payload["message"]
    assert names[-1] == "error"

    # The error branch must clear the typing indicator before breaking out —
    # otherwise other group members see the agent typing forever (the outer
    # ``finally``/``stream_end`` cleanup never runs on this path because
    # ``gen()`` breaks on the error frame).
    assert any(active is False for _, active in typing_calls), (
        "typing indicator left ON after backend error — group members would "
        "see the agent typing indefinitely"
    )
