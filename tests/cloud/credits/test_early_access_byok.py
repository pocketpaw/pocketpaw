# tests/cloud/credits/test_early_access_byok.py: who pays is decided before the
# credit gate. A workspace with its own stored key (the Ripple guest handoff,
# the Otherhand kiosk) runs at a wallet of 0 and its run debits nothing, while a
# resolver that crashes leaves the turn gated and charged as platform spend.
#
# Same posture and harness as ``test_early_access_cap.py`` (its fixtures reach
# this file through ``conftest.py``)
# (``credits_enforced=True, billing_enforced=False``, real ledger, real
# ``bill_run``, real guard). The key row is real too: it is encrypted with a
# per-test Fernet key and read back by the real ``resolve_turn_credentials``.
# Only infrastructure is stubbed: scope resolution, the Redis stream
# (fakeredis), and the agent event source, which stands in for the model and
# reports a priced ``token_usage`` so the zero debit is not an unpriced run.
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import fakeredis.aioredis
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from tests.cloud.credits.test_early_access_cap import (
    PRICED_MODEL,
    _drain_stream,
    _EndingTransport,
    _rate_card,
    drain_wallet,
)

pytestmark = pytest.mark.asyncio

_KEY = "sk-ant-api03-" + "byokgate" * 6
_USAGE = {"input_tokens": 10_000, "output_tokens": 2_000, "model": PRICED_MODEL}


async def _store_key(monkeypatch, workspace_id: str) -> None:
    """Store a real, encrypted anthropic key for ``workspace_id``."""
    from pocketpaw_ee.cloud.byok import service as byok_service

    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    await byok_service.set_key(workspace_id, _KEY, provider="anthropic", validate=False)


def _wire_executor(monkeypatch, ws: str, uid: str) -> tuple[Any, list[Any]]:
    """Stub scope resolution, the stream and the agent; return (transport, seen ctx)."""
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

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
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _none)
    monkeypatch.setattr(run_core, "_prewarm_session", _none)
    monkeypatch.setattr("pocketpaw_ee.cloud.sessions.service.ensure_for_agent_scope", _none)

    seen: list[Any] = []

    async def _agent_events(_spec, ctx):
        seen.append(ctx)
        yield ("chunk", {"content": "hi back"})
        yield ("token_usage", dict(_USAGE))

    async def _persist(spec, _ctx, full_text, _attachments, usage=None, **_k):
        await run_service.mark_completed(
            spec.run_id, assistant_message_id=None, partial_text=full_text, usage=usage
        )

    monkeypatch.setattr(run_core, "_iter_agent_events", _agent_events)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist)
    return transport, seen


def _spec(ws: str, uid: str, run_id: str):
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

    return RunSpec(
        run_id=run_id,
        workspace_id=ws,
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id=uid,
        agent_id="a1",
        client_message_id=f"c-{run_id}",
        user_message_id=f"m-{run_id}",
        content="hello",
        history=[],
        intent=None,
    )


async def _terminal(transport: Any, run_id: str) -> Any:
    events = [ev async for ev in _drain_stream(transport, run_id)]
    terminal = [ev for ev in events if ev.event in ("error", "stream_end")]
    assert len(terminal) == 1, [ev.event for ev in events]
    return terminal[0]


# ---------------------------------------------------------------------------
# Seam 1: execute_run
# ---------------------------------------------------------------------------


async def test_byok_run_at_wallet_zero_proceeds_and_bills_zero(
    ea_user, early_access_posture, monkeypatch
):
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.metering import service as metering
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    ws, uid = ea_user.workspace_id, ea_user.user_id
    await drain_wallet(ws, uid)
    balance_before = await credits_service.balance_micro(ws)
    assert balance_before <= 0
    await _store_key(monkeypatch, ws)
    transport, seen = _wire_executor(monkeypatch, ws, uid)
    monkeypatch.setattr(metering, "load_rate_card", _rate_card)

    spec = _spec(ws, uid, "r-byok")
    await run_service.create_run(spec)
    await run_core.execute_run(spec)

    end = await _terminal(transport, spec.run_id)
    assert end.event == "stream_end", end.data
    # The loop got the credentials the gate resolved, not a second resolution.
    assert len(seen) == 1
    assert seen[0].turn_credentials.source == "byok"
    assert seen[0].turn_credentials.api_key == _KEY

    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == spec.run_id)
    assert doc is not None and doc.status == "completed"
    assert doc.credential_source == "byok"
    assert doc.usage["input_tokens"] == _USAGE["input_tokens"]

    result = await metering.bill_run_now(spec.run_id)
    assert result is not None
    # Priced usage, so a platform run would have debited; a byok run must not.
    assert result.cost_usd > 0
    assert result.debited is False
    assert result.micro_charged == 0
    assert await credits_service.balance_micro(ws) == balance_before
    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == spec.run_id)
    assert doc.billed is True


async def test_resolver_failure_is_gated_and_charged_as_platform(
    ea_user, early_access_posture, backend_factory_spy, monkeypatch
):
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.metering import service as metering
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    ws, uid = ea_user.workspace_id, ea_user.user_id
    await drain_wallet(ws, uid)
    # A real key is stored, so only the crash keeps this turn off BYOK.
    await _store_key(monkeypatch, ws)

    async def _boom(_workspace_id):
        raise RuntimeError("resolver down")

    monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.resolve_turn_credentials", _boom)
    transport, seen = _wire_executor(monkeypatch, ws, uid)

    spec = _spec(ws, uid, "r-resolver-down")
    await run_service.create_run(spec)
    await run_core.execute_run(spec)

    end = await _terminal(transport, spec.run_id)
    assert end.event == "error"
    assert end.data["code"] == "credits.insufficient"
    assert seen == []
    assert backend_factory_spy == []
    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == spec.run_id)
    assert doc is not None and doc.status == "failed"
    assert doc.credential_source == "platform"

    # The charge half: a platform run with priced usage debits.
    doc.usage = dict(_USAGE)
    await doc.save()
    result = await metering.bill_run(doc, rate_card=_rate_card())
    assert result.debited is True and result.micro_charged > 0


async def test_gateway_egress_rejection_refuses_the_turn_not_the_wallet(
    ea_user, early_access_posture, backend_factory_spy, monkeypatch
):
    from pocketpaw_ee.cloud.byok.service import GatewayEgressRejected
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    ws, uid = ea_user.workspace_id, ea_user.user_id

    async def _rejected(_workspace_id):
        raise GatewayEgressRejected("That gateway address is not allowed.")

    monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.resolve_turn_credentials", _rejected)
    transport, seen = _wire_executor(monkeypatch, ws, uid)

    spec = _spec(ws, uid, "r-egress")
    await run_service.create_run(spec)
    await run_core.execute_run(spec)

    end = await _terminal(transport, spec.run_id)
    assert end.event == "error"
    assert end.data["code"] == "byok.base_url_rejected"
    assert seen == []
    assert backend_factory_spy == []
    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == spec.run_id)
    assert doc is not None and doc.status == "failed"


# ---------------------------------------------------------------------------
# Seam 2: chat HTTP fast-reject
# ---------------------------------------------------------------------------


async def _post_turn(monkeypatch, ws: str, uid: str) -> tuple[Any, list[str]]:
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

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
            json={"content": "hello", "client_message_id": "c-http-byok"},
        )
    return resp, submitted


async def test_byok_chat_http_at_wallet_zero_is_admitted(
    ea_user, early_access_posture, monkeypatch
):
    ws, uid = ea_user.workspace_id, ea_user.user_id
    await drain_wallet(ws, uid)
    await _store_key(monkeypatch, ws)

    resp, submitted = await _post_turn(monkeypatch, ws, uid)

    assert resp.status_code == 200, resp.text
    assert len(submitted) == 1


async def test_chat_http_resolver_failure_returns_402(
    ea_user, early_access_posture, backend_factory_spy, monkeypatch
):
    ws, uid = ea_user.workspace_id, ea_user.user_id
    await drain_wallet(ws, uid)
    await _store_key(monkeypatch, ws)

    async def _boom(_workspace_id):
        raise RuntimeError("resolver down")

    monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.resolve_turn_credentials", _boom)

    resp, submitted = await _post_turn(monkeypatch, ws, uid)

    assert resp.status_code == 402
    assert resp.json()["error"]["code"] == "credits.insufficient"
    assert submitted == []
    assert backend_factory_spy == []
