"""POST /cloud/chat/{scope}/{scope_id}/agent and its /agent/stop sibling.

Covers the SSE shape, idempotent run creation, and the supersede/stop races:
a new message waits for the run it supersedes to finish before loading history,
a stop that lands before the run exists still cancels it, and only a caller who
owns the run or can access the scope may stop it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport

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

    async def request_scope_stop(self, scope_key: str, ttl_seconds: int) -> None:  # noqa: ARG002
        return None

    async def scope_stop_token(self, scope_key: str) -> str | None:  # noqa: ARG002
        return None

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

    class _CancelTrackingTransport(InMemoryStreamTransport):
        async def request_cancel(self, run_id: str) -> None:
            cancelled.append(run_id)
            await super().request_cancel(run_id)

    monkeypatch.setattr(mod, "get_stream_transport", lambda: _CancelTrackingTransport())

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

    class _CancelTrackingTransport(InMemoryStreamTransport):
        async def request_cancel(self, run_id: str) -> None:
            cancelled.append(run_id)
            await super().request_cancel(run_id)

    monkeypatch.setattr(mod, "get_stream_transport", lambda: _CancelTrackingTransport())

    with patch.object(mod, "resolve_scope_context", _fake_resolve):
        resp = await cloud_app_client.post("/cloud/chat/session/s-idle/agent/stop")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert cancelled == []


# ---------------------------------------------------------------------------
# Supersede: a new message waits for the run it cancels
# ---------------------------------------------------------------------------


def _run_spec(run_id: str, *, client_message_id: str, user_id: str = "u1"):
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

    return RunSpec(
        run_id=run_id,
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="k1",
        group=None,
        user_id=user_id,
        agent_id="a1",
        client_message_id=client_message_id,
        user_message_id="um-prior",
        content="first question",
        history=[],
        intent=None,
    )


class _SlowCancelTransport(InMemoryStreamTransport):
    """A transport whose cancelled run takes a moment to wind down, the way a
    worker polling the Redis flag does: it writes the terminal status ``delay``
    seconds after the cancel is requested."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self.tasks: list = []

    async def request_cancel(self, run_id: str) -> None:
        import asyncio

        from pocketpaw_ee.cloud.chat.runs import service as run_service

        await super().request_cancel(run_id)
        if run_id != "run-prior":
            return

        async def _wind_down() -> None:
            await asyncio.sleep(self.delay)
            await run_service.mark_terminal(
                run_id, status="cancelled", partial_text="half an answer"
            )

        self.tasks.append(asyncio.create_task(_wind_down()))


async def _post_with_status_probe(cloud_app_client, monkeypatch, transport):
    """POST a new message while ``run-prior`` is active; return the prior run's
    status as ``load_history_for_scope`` saw it."""
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    await run_service.create_run(_run_spec("run-prior", client_message_id="c-prior"))
    seen: list[str] = []

    async def _probing_history(_ctx, *, limit=50):  # noqa: ARG001
        seen.append((await run_service.get_run("run-prior")).status)
        return []

    class _EndingExecutor:
        async def submit(self, spec):
            await transport.append_event(spec.run_id, "stream_end", {"cancelled": False})

    monkeypatch.setattr(mod, "get_executor", lambda: _EndingExecutor())
    monkeypatch.setattr(mod, "get_stream_transport", lambda: transport)

    with (
        patch.object(mod, "resolve_scope_context", _fake_resolve),
        patch.object(mod, "load_history_for_scope", _probing_history),
        patch.object(mod, "_persist_user_message", _fake_persist_user_message),
        patch.object(mod, "_ensure_scope_session", _fake_ensure_session),
    ):
        resp = await cloud_app_client.post(
            "/cloud/chat/session/s1/agent",
            json={"content": "second question", "client_message_id": "c-new"},
        )
    assert resp.status_code == 200
    return seen


async def test_supersede_waits_for_prior_run_before_loading_history(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """The cancelled run writes its partial reply on its way out. History must
    be loaded after that, or the model sees two user turns with no reply between
    them, and the two runs overlap on the same scope."""
    transport = _SlowCancelTransport(delay=0.3)
    seen = await _post_with_status_probe(cloud_app_client, monkeypatch, transport)
    assert seen == ["cancelled"]


async def test_supersede_gives_up_waiting_after_the_timeout(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001
    monkeypatch,
    caplog,
):
    """A prior run that never winds down must not wedge the new message: the
    wait is bounded and the new turn goes ahead with a warning."""
    from pocketpaw_ee.cloud.chat import agent_router as mod

    monkeypatch.setattr(mod, "SUPERSEDE_WAIT_SECONDS", 0.2)
    transport = _SlowCancelTransport(delay=60)
    with caplog.at_level("WARNING", logger=mod.__name__):
        seen = await _post_with_status_probe(cloud_app_client, monkeypatch, transport)
    for t in transport.tasks:
        t.cancel()
    assert seen == ["queued"]
    assert any("run-prior" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Stop before the run exists
# ---------------------------------------------------------------------------


async def _post_with_stop_hook(cloud_app_client, monkeypatch, *, stop_during: bool):
    """POST a message. With ``stop_during``, /agent/stop runs from inside the
    request after the user message is persisted and before ``create_run``, the
    window where a stop used to find no run to cancel."""
    from pocketpaw_ee.cloud.chat import agent_router as mod

    transport = InMemoryStreamTransport()

    async def _persist_then_stop(ctx, body):
        if stop_during:
            await mod.post_agent_chat_stop(
                scope="session", scope_id="s1", user_id="u1", workspace_id="w1"
            )
        return await _fake_persist_user_message(ctx, body)

    class _EndingExecutor:
        async def submit(self, spec):
            if await transport.is_cancelled(spec.run_id):
                await transport.append_event(spec.run_id, "interrupted", {})
            else:
                await transport.append_event(spec.run_id, "stream_end", {"cancelled": False})

    monkeypatch.setattr(mod, "get_executor", lambda: _EndingExecutor())
    monkeypatch.setattr(mod, "get_stream_transport", lambda: transport)

    with (
        patch.object(mod, "resolve_scope_context", _fake_resolve),
        patch.object(mod, "load_history_for_scope", _fake_load_history),
        patch.object(mod, "_persist_user_message", _persist_then_stop),
        patch.object(mod, "_ensure_scope_session", _fake_ensure_session),
    ):
        resp = await cloud_app_client.post(
            "/cloud/chat/session/s1/agent",
            json={"content": "hello", "client_message_id": "c-stop"},
        )
    assert resp.status_code == 200
    frames = _parse_sse(resp.content)
    return transport, frames[0][1]["run_id"], frames


async def test_stop_before_run_is_created_still_cancels_it(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    transport, run_id, frames = await _post_with_stop_hook(
        cloud_app_client, monkeypatch, stop_during=True
    )
    assert await transport.is_cancelled(run_id)
    assert frames[-1][0] == "interrupted"


async def test_stop_while_idle_does_not_cancel_the_next_message(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """A stop recorded before a message was sent cannot reach that message; the
    pending stop only catches requests already in flight."""
    from pocketpaw_ee.cloud.chat import agent_router as mod

    shared = InMemoryStreamTransport()
    monkeypatch.setattr(mod, "get_stream_transport", lambda: shared)
    with patch.object(mod, "resolve_scope_context", _fake_resolve):
        resp = await cloud_app_client.post("/cloud/chat/session/s1/agent/stop")
    assert resp.json() == {"status": "ok"}

    class _EndingExecutor:
        async def submit(self, spec):
            await shared.append_event(spec.run_id, "stream_end", {"cancelled": False})

    monkeypatch.setattr(mod, "get_executor", lambda: _EndingExecutor())
    with (
        patch.object(mod, "resolve_scope_context", _fake_resolve),
        patch.object(mod, "load_history_for_scope", _fake_load_history),
        patch.object(mod, "_persist_user_message", _fake_persist_user_message),
        patch.object(mod, "_ensure_scope_session", _fake_ensure_session),
    ):
        resp = await cloud_app_client.post(
            "/cloud/chat/session/s1/agent",
            json={"content": "hello", "client_message_id": "c-after-idle-stop"},
        )
    frames = _parse_sse(resp.content)
    assert not await shared.is_cancelled(frames[0][1]["run_id"])
    assert frames[-1][0] == "stream_end"


# ---------------------------------------------------------------------------
# Who may stop a run
# ---------------------------------------------------------------------------


async def test_stop_refuses_a_caller_without_scope_access(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    """Someone else's run in a scope the caller cannot open is not theirs to stop."""
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    await run_service.create_run(_run_spec("run-other", client_message_id="c-o", user_id="u2"))
    transport = InMemoryStreamTransport()
    monkeypatch.setattr(mod, "get_stream_transport", lambda: transport)

    async def _deny(**_):
        raise mod.CloudError(403, "session.forbidden", "Caller does not own this session")

    with patch.object(mod, "resolve_scope_context", _deny):
        resp = await cloud_app_client.post("/cloud/chat/session/s1/agent/stop")
    assert resp.status_code == 403
    assert not await transport.is_cancelled("run-other")


async def test_stop_lets_a_scope_member_stop_another_members_run(
    cloud_app_client: AsyncClient,
    mongo_db,  # noqa: ARG001
    monkeypatch,
):
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    await run_service.create_run(_run_spec("run-other", client_message_id="c-o", user_id="u2"))
    transport = InMemoryStreamTransport()
    monkeypatch.setattr(mod, "get_stream_transport", lambda: transport)

    with patch.object(mod, "resolve_scope_context", _fake_resolve):
        resp = await cloud_app_client.post("/cloud/chat/session/s1/agent/stop")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    assert await transport.is_cancelled("run-other")
