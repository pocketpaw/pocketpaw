# tests/cloud/credits/test_early_access_cap.py: the early-access cap test. A
# workspace granted N early access credits is drained by real billed runs, and
# the next turn is refused with ``credits.insufficient`` on both seams (the chat
# HTTP fast-reject and ``execute_run``) without the model ever being called.
#
# Runs with ``credits_enforced=True, billing_enforced=False``: the early-access
# posture, where only the chat credit gate is on and every other billing gate
# stays off. Nothing on the money path is faked. The ledger, ``bill_run``, the
# guard and ``check_balance`` / ``check_quota`` are all real; the drain uses a
# priced model so every bill moves money (``BillResult.debited``). Only true
# infrastructure is stubbed: scope resolution, the Redis stream (fakeredis) and
# the backend factory, which is replaced by a spy that proves no model was built.
#
# The fixtures here (``ea_user``, ``drain_wallet``, ``backend_factory_spy``) are
# the shared harness the later early-access guards reuse.
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio

# The launch default chat model. It is in the pricing table, so a run carrying
# its token counts bills a real, non-zero amount.
PRICED_MODEL = "claude-haiku-4-5"
GRANT = 10


def _rate_card():
    from pocketpaw_ee.cloud.metering.domain import RateCard

    # Pinned so the drain does not depend on ambient settings: 1 credit = $0.01.
    return RateCard(markup=2.5, credit_usd=0.01)


def _early_access_settings():
    """Real settings with the early-access posture: credits gate on, billing off."""
    from pocketpaw.config import get_settings

    return get_settings().model_copy(update={"credits_enforced": True, "billing_enforced": False})


@pytest.fixture
def early_access_posture(monkeypatch):
    """Point the guard's settings source at the early-access posture."""
    from pocketpaw_ee.cloud.credits import guards

    settings = _early_access_settings()
    monkeypatch.setattr(guards, "get_settings", lambda: settings)
    return settings


@pytest_asyncio.fixture
async def ea_user(mongo_db) -> SimpleNamespace:  # noqa: ARG001 (forces Beanie init)
    """A fresh verified user who owns a free workspace, granted ``GRANT`` credits
    the way the early-access grant will: ``kind='genesis'`` keyed per user."""
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
    from pocketpaw_ee.cloud.models.workspace import Workspace

    tag = uuid.uuid4().hex[:8]
    user = User(
        email=f"ea-{tag}@example.com",
        hashed_password="x",
        is_active=True,
        is_verified=True,
    )
    await user.insert()
    user_id = str(user.id)
    ws = Workspace(name=f"EA {tag}", slug=f"ea-{tag}", owner=user_id, plan="free")
    await ws.insert()
    workspace_id = str(ws.id)
    user.active_workspace = workspace_id
    user.workspaces = [WorkspaceMembership(workspace=workspace_id, role="owner")]
    await user.save()

    result = await credits_service.grant(
        workspace_id,
        GRANT,
        cause="early_access",
        idempotency_key=f"early_access:{user_id}",
        kind="genesis",
    )
    assert result.created is True
    return SimpleNamespace(user_id=user_id, workspace_id=workspace_id)


async def drain_wallet(workspace_id: str, user_id: str) -> int:
    """Bill priced runs through the real ``bill_run`` until the wallet is <= 0.

    Every bill must move money: an unpriced run reports ``debited=False`` and
    would let this loop "drain" nothing. Returns the number of runs billed.
    """
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.metering import service as metering
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    billed = 0
    while await credits_service.balance_micro(workspace_id) > 0:
        assert billed < 20, "drain did not converge; is the model priced?"
        run_id = f"drain-{uuid.uuid4().hex[:8]}"
        doc = ChatRunDoc(
            run_id=run_id,
            workspace=workspace_id,
            context_type="session",
            scope_id="s-drain",
            session_key="session:s-drain",
            user_id=user_id,
            agent_id="a1",
            client_message_id=f"c-{run_id}",
            user_message_id=f"m-{run_id}",
            status="completed",
            usage={"input_tokens": 10_000, "output_tokens": 2_000, "model": PRICED_MODEL},
        )
        await doc.insert()
        result = await metering.bill_run(doc, rate_card=_rate_card())
        assert result.debited is True
        assert result.micro_charged > 0
        billed += 1
    return billed


@pytest.fixture
def backend_factory_spy(monkeypatch) -> list[str]:
    """Replace the agent backend factory with a spy that refuses to build one.

    Covers both entry points: the agent pool ``run_core`` drives (and prewarms
    through) and the registry's ``get_backend_class`` the pool and router use to
    construct a backend. Any call is recorded; the test asserts the list is empty.
    """
    from pocketpaw_ee.cloud.chat.runs import run_core

    from pocketpaw.agents import registry
    from pocketpaw.agents import router as agents_router

    calls: list[str] = []

    def _no_backend(name: str = "", *a: Any, **k: Any):
        calls.append(f"get_backend_class:{name}")
        raise AssertionError("backend factory called on a capped workspace")

    def _no_pool():
        calls.append("get_agent_pool")
        raise AssertionError("agent pool reached on a capped workspace")

    monkeypatch.setattr(registry, "get_backend_class", _no_backend)
    monkeypatch.setattr(agents_router, "get_backend_class", _no_backend)
    monkeypatch.setattr(run_core, "get_agent_pool", _no_pool)
    return calls


# ---------------------------------------------------------------------------
# Seam 1: execute_run (worker / executor path)
# ---------------------------------------------------------------------------


async def test_drained_wallet_execute_run_refuses_without_model_call(
    ea_user, early_access_posture, backend_factory_spy, monkeypatch
):
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
    from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    assert early_access_posture.billing_enforced is False
    ws, uid = ea_user.workspace_id, ea_user.user_id

    assert await drain_wallet(ws, uid) >= 1
    assert await credits_service.balance_micro(ws) <= 0

    transport = RedisStreamTransport(fakeredis.aioredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)

    async def _ctx(**_):
        return SimpleNamespace(
            kind=SimpleNamespace(value="session"),
            scope_id="s1",
            workspace_id=ws,
            user_id=uid,
            target_agent_id="a1",
            members=[uid],
            session_id=None,
            intent=None,
            surface_context=None,
            resolved_profile=None,
            pocket_id=None,
        )

    async def _none(*_a, **_k):
        return None

    monkeypatch.setattr(run_core, "resolve_scope_context", _ctx)
    monkeypatch.setattr(run_core, "resolve_surface_context", _none)
    monkeypatch.setattr(run_core, "_resolve_entity_profile", _none)
    monkeypatch.setattr("pocketpaw_ee.cloud.sessions.service.ensure_for_agent_scope", _none)

    spec = RunSpec(
        run_id="r-capped",
        workspace_id=ws,
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id=uid,
        agent_id="a1",
        client_message_id="c-capped",
        user_message_id="m-capped",
        content="hello",
        history=[],
        intent=None,
    )
    await run_service.create_run(spec)

    await run_core.execute_run(spec)

    events = [ev async for ev in _drain_stream(transport, spec.run_id)]
    terminal = [ev for ev in events if ev.event in ("error", "stream_end")]
    assert len(terminal) == 1, [ev.event for ev in events]
    assert terminal[0].event == "error"
    assert terminal[0].data["code"] == "credits.insufficient"
    assert terminal[0].data["message"]

    assert backend_factory_spy == []
    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == spec.run_id)
    assert doc is not None and doc.status == "failed"


async def _drain_stream(transport: Any, run_id: str) -> AsyncIterator[Any]:
    """Read every event already on the run's stream, without blocking."""
    async for ev in transport.read_events(run_id, after="0", block_ms=1):
        yield ev
        if ev.event in ("error", "stream_end"):
            return


# ---------------------------------------------------------------------------
# Seam 2: chat HTTP fast-reject
# ---------------------------------------------------------------------------


class _EndingTransport:
    """Ends the SSE stream at once, so a turn the gate wrongly admits finishes
    with a 200 instead of hanging on a stream nobody writes to."""

    async def request_cancel(self, run_id: str) -> None:  # noqa: ARG002
        return None

    async def scope_stop_token(self, scope_key: str) -> str | None:  # noqa: ARG002
        return None

    def read_events(self, run_id: str, *, after: str = "0", block_ms: int = 15000):  # noqa: ARG002
        async def _gen():
            from pocketpaw_ee.cloud.chat.runs.transport import StreamEvent

            yield StreamEvent(entry_id="1-0", event="stream_end", data={"usage": {}})

        return _gen()


async def test_drained_wallet_chat_http_returns_402_with_code(
    ea_user, early_access_posture, backend_factory_spy, monkeypatch
):
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    assert early_access_posture.credits_enforced is True
    ws, uid = ea_user.workspace_id, ea_user.user_id
    await drain_wallet(ws, uid)
    runs_before = await ChatRunDoc.find_all().count()

    submitted: list[str] = []

    class _Executor:
        async def submit(self, spec):
            submitted.append(spec.run_id)

    async def _ctx(**_):
        return SimpleNamespace(
            kind=SimpleNamespace(value="session"),
            scope_id="s1",
            workspace_id=ws,
            user_id=uid,
            members=[uid],
            target_agent_id="a1",
            agent_ids_in_scope=["a1"],
            pocket_tool_specs=[],
            session_id=None,
            pocket_id=None,
            intent=None,
        )

    async def _history(_ctx, *, limit=50):  # noqa: ARG001
        return []

    async def _persist(_ctx, _body):
        return "user-msg-1"

    async def _session(_ctx):
        return "session-1"

    monkeypatch.setattr(mod, "get_executor", lambda: _Executor())
    # Only reached if the gate lets the turn through; keeps that failure an
    # assertion on the status code rather than a Redis connection error.
    monkeypatch.setattr(mod, "get_stream_transport", lambda: _EndingTransport())
    monkeypatch.setattr(mod, "resolve_scope_context", _ctx)
    monkeypatch.setattr(mod, "load_history_for_scope", _history)
    monkeypatch.setattr(mod, "_persist_user_message", _persist)
    monkeypatch.setattr(mod, "_ensure_scope_session", _session)

    app = FastAPI()
    add_error_handler(app)
    app.include_router(mod.router)
    app.dependency_overrides[current_user_id] = lambda: uid
    app.dependency_overrides[current_workspace_id] = lambda: ws
    app.dependency_overrides[require_license] = lambda: None

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.post(
            "/cloud/chat/session/s1/agent",
            json={"content": "hello", "client_message_id": "c-http"},
        )

    assert resp.status_code == 402
    error = resp.json()["error"]
    assert error["code"] == "credits.insufficient"
    assert error["message"]
    assert submitted == []
    assert await ChatRunDoc.find_all().count() == runs_before
    assert backend_factory_spy == []
