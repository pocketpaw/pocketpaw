"""POST /cloud/chat/{scope}/{scope_id}/agent."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


def _fake_ctx() -> SimpleNamespace:
    """Minimal ``ScopeContext`` stand-in good enough for the router's reads."""
    return SimpleNamespace(
        kind=SimpleNamespace(value="session"),
        scope_id="s1",
        workspace_id="w1",
        user_id="u1",
        members=["u1"],
        target_agent_id="a1",
        agent_ids_in_scope=["a1"],
        pocket_tool_specs=[],
        session_id=None,
        pocket_id=None,
        intent=None,
    )


async def _fake_resolve(**_):
    return _fake_ctx()


async def _fake_persist_user_message(_ctx, _body):
    return "user_msg_id_1"


async def _fake_load_history(_ctx, *, limit=50):  # noqa: ARG001
    return []


async def _fake_ensure_session(_ctx):
    return "session_id_1"


class _StubTransport:
    """Transport that emits one ``stream_end`` immediately so the SSE
    generator terminates without needing a real Redis."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []

    async def request_cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    def read_events(self, run_id: str, *, after: str = "0", block_ms: int = 15000) -> AsyncIterator:  # noqa: ARG002
        async def _gen() -> AsyncIterator:
            from pocketpaw_ee.cloud.chat.runs.transport import StreamEvent

            yield StreamEvent(
                entry_id="1-0",
                event="stream_end",
                data={"assistant_message_id": None, "usage": {}, "cancelled": False},
            )

        return _gen()


def _parse_sse(body: bytes) -> list[tuple[str, dict]]:
    """Parse ``event:``/``data:`` SSE frames from a response body."""
    out: list[tuple[str, dict]] = []
    for part in body.decode().split("\n\n"):
        event = ""
        data = ""
        for line in part.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if event and data:
            out.append((event, json.loads(data)))
    return out


async def test_post_agent_streams_sse_with_message_persisted_first(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — forces Beanie init so create_run can persist
    monkeypatch,
):
    """POST streams SSE: first frame is ``message.persisted`` with the user
    message id + the just-created run id, then frames flow from the run's
    transport until a terminal event closes the response."""
    from pocketpaw_ee.cloud.chat import agent_router as mod

    submitted: list[str] = []

    class _FakeExecutor:
        async def submit(self, spec):
            submitted.append(spec.run_id)

    monkeypatch.setattr(mod, "get_executor", lambda: _FakeExecutor())
    monkeypatch.setattr(mod, "get_stream_transport", lambda: _StubTransport())

    with (
        patch.object(mod, "resolve_scope_context", _fake_resolve),
        patch.object(mod, "load_history_for_scope", _fake_load_history),
        patch.object(mod, "_persist_user_message", _fake_persist_user_message),
        patch.object(mod, "_ensure_scope_session", _fake_ensure_session),
    ):
        resp = await cloud_app_client.post(
            "/cloud/chat/session/s1/agent",
            json={"content": "hello", "client_message_id": "c1"},
        )

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    frames = _parse_sse(resp.content)
    assert frames, "expected at least the message.persisted frame"
    assert frames[0][0] == "message.persisted"
    persisted = frames[0][1]
    assert persisted["user_message_id"] == "user_msg_id_1"
    assert persisted["client_message_id"] == "c1"
    assert persisted["session_id"] == "session_id_1"
    assert persisted["run_id"]
    assert frames[-1][0] == "stream_end"
    # The executor received the freshly created run.
    assert submitted == [persisted["run_id"]]


async def test_post_agent_idempotent_on_client_message_id(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001 — forces Beanie init so create_run can persist
    monkeypatch,
):
    """Two POSTs with the same ``client_message_id`` resolve to one run."""
    from pocketpaw_ee.cloud.chat import agent_router as mod

    submitted: list[str] = []

    class _FakeExecutor:
        async def submit(self, spec):
            submitted.append(spec.run_id)

    monkeypatch.setattr(mod, "get_executor", lambda: _FakeExecutor())
    monkeypatch.setattr(mod, "get_stream_transport", lambda: _StubTransport())

    with (
        patch.object(mod, "resolve_scope_context", _fake_resolve),
        patch.object(mod, "load_history_for_scope", _fake_load_history),
        patch.object(mod, "_persist_user_message", _fake_persist_user_message),
        patch.object(mod, "_ensure_scope_session", _fake_ensure_session),
    ):
        body_json = {"content": "hi", "client_message_id": "same"}
        r1 = await cloud_app_client.post("/cloud/chat/session/s1/agent", json=body_json)
        r2 = await cloud_app_client.post("/cloud/chat/session/s1/agent", json=body_json)

    assert r1.status_code == 200 and r2.status_code == 200
    run_id_1 = _parse_sse(r1.content)[0][1]["run_id"]
    run_id_2 = _parse_sse(r2.content)[0][1]["run_id"]
    assert run_id_1 == run_id_2, (
        "create_run is idempotent on (workspace, client_message_id), so a "
        "re-submitted message must return the same run."
    )


async def test_post_agent_stop_cancels_active_run(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """POST /agent/stop calls request_cancel on the active run for the scope."""
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

    spec = RunSpec(
        run_id="run-to-cancel",
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="k1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="um1",
        content="hi",
        history=[],
        intent=None,
        attachments=[],
        mentions=[],
        reply_to=None,
    )
    await run_service.create_run(spec)

    cancelled: list[str] = []

    class _StubTransport:
        async def request_cancel(self, run_id: str) -> None:
            cancelled.append(run_id)

    monkeypatch.setattr(mod, "get_stream_transport", lambda: _StubTransport())

    resp = await cloud_app_client.post("/cloud/chat/session/s1/agent/stop")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert cancelled == ["run-to-cancel"]


async def test_post_agent_stop_is_noop_when_no_active_run(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """No active run for scope → still returns ok (idempotent)."""
    from pocketpaw_ee.cloud.chat import agent_router as mod

    cancelled: list[str] = []

    class _StubTransport:
        async def request_cancel(self, run_id: str) -> None:
            cancelled.append(run_id)

    monkeypatch.setattr(mod, "get_stream_transport", lambda: _StubTransport())

    resp = await cloud_app_client.post("/cloud/chat/session/s-nonexistent/agent/stop")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert cancelled == []
