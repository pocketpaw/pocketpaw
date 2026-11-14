# tests/cloud/credits/test_launch_controls.py: the early-access kill switch.
#
# With ``free_usage_paused`` on, an unpaid early-access run is refused with 503
# ``early_access.paused`` on both seams (the chat HTTP fast-reject and
# ``execute_run``) before any model is built, while a paid workspace still runs.
# The switch is read through a 30 s per-process cache (a fake clock proves a
# flip made elsewhere shows up after the TTL and not before), a failed read fails
# open, and the operator endpoint writes the row and audits it.
#
# Real ledger, real guard, real launch-controls service on the in-memory Mongo.
# The cap-test harness (``ea_user``, ``backend_factory_spy``) is reused from
# test_early_access_cap.py.
from __future__ import annotations

import logging
from types import SimpleNamespace

import fakeredis.aioredis
import pytest
from httpx import ASGITransport, AsyncClient

from tests.cloud.credits.test_early_access_cap import (  # noqa: F401 (fixtures)
    _drain_stream,
    _EndingTransport,
    backend_factory_spy,
    ea_user,
    early_access_posture,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_cache():
    """The snapshot is per process; never let one test read another's."""
    from pocketpaw_ee.cloud.launch_controls import service

    service.invalidate_cache()
    yield
    service.invalidate_cache()


async def _pause(paused: bool = True) -> None:
    from pocketpaw_ee.cloud.launch_controls import service

    await service.update_launch_controls({"free_usage_paused": paused}, operator_id="op-test")


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


async def test_pause_refuses_unpaid_workspace(ea_user, early_access_posture):  # noqa: F811
    from pocketpaw_ee.cloud.credits.guards import over_billing_limit

    # The wallet holds credits, so only the kill switch can refuse.
    assert await over_billing_limit(ea_user.workspace_id) is None
    await _pause()

    exc = await over_billing_limit(ea_user.workspace_id)
    assert exc is not None
    assert exc.status_code == 503
    assert exc.code == "early_access.paused"
    assert exc.message


async def test_pause_passes_paid_plan_workspace(ea_user, early_access_posture):  # noqa: F811
    from pocketpaw_ee.cloud.credits.guards import over_billing_limit
    from pocketpaw_ee.cloud.models.workspace import Workspace

    ws = await Workspace.get(ea_user.workspace_id)
    ws.plan = "pro"
    await ws.save()
    await _pause()

    assert await over_billing_limit(ea_user.workspace_id) is None


async def test_pause_passes_free_plan_with_top_up(ea_user, early_access_posture):  # noqa: F811
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.credits.guards import over_billing_limit

    await credits_service.grant(
        ea_user.workspace_id, 100, cause="top_up", idempotency_key="tu-1", kind="top_up"
    )
    await _pause()

    assert await over_billing_limit(ea_user.workspace_id) is None


async def test_unpause_lets_unpaid_workspace_run_again(ea_user, early_access_posture):  # noqa: F811
    from pocketpaw_ee.cloud.credits.guards import over_billing_limit

    await _pause(True)
    assert (await over_billing_limit(ea_user.workspace_id)).code == "early_access.paused"
    await _pause(False)
    assert await over_billing_limit(ea_user.workspace_id) is None


# ---------------------------------------------------------------------------
# Seam 1: execute_run (worker path)
# ---------------------------------------------------------------------------


async def test_pause_execute_run_refuses_without_model_call(
    ea_user,  # noqa: F811
    early_access_posture,  # noqa: F811
    backend_factory_spy,  # noqa: F811
    monkeypatch,
):
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
    from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    ws, uid = ea_user.workspace_id, ea_user.user_id
    await _pause()

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
        run_id="r-paused",
        workspace_id=ws,
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id=uid,
        agent_id="a1",
        client_message_id="c-paused",
        user_message_id="m-paused",
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
    assert terminal[0].data["code"] == "early_access.paused"
    assert backend_factory_spy == []
    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == spec.run_id)
    assert doc is not None and doc.status == "failed"


# ---------------------------------------------------------------------------
# Seam 2: chat HTTP fast-reject
# ---------------------------------------------------------------------------


async def test_pause_chat_http_returns_503_with_code(
    ea_user,  # noqa: F811
    early_access_posture,  # noqa: F811
    backend_factory_spy,  # noqa: F811
    monkeypatch,
):
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.chat import agent_router as mod
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.shared.deps import current_user_id, current_workspace_id

    ws, uid = ea_user.workspace_id, ea_user.user_id
    await _pause()
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
            json={"content": "hello", "client_message_id": "c-http"},
        )

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "early_access.paused"
    assert submitted == []
    assert backend_factory_spy == []


# ---------------------------------------------------------------------------
# Cache and read failure
# ---------------------------------------------------------------------------


async def test_flip_elsewhere_is_seen_after_the_ttl_not_before(mongo_db, monkeypatch):
    from pocketpaw_ee.cloud.launch_controls import service
    from pocketpaw_ee.cloud.models.launch_controls import LaunchControlsDoc

    now = [1000.0]
    monkeypatch.setattr(service, "_clock", lambda: now[0])

    assert (await service.get_launch_controls()).free_usage_paused is False

    # Another process (or an operator in Mongo) flips the row; this process's
    # snapshot is untouched, as on a worker.
    await LaunchControlsDoc(free_usage_paused=True).insert()

    now[0] += service.CACHE_TTL_SECONDS - 1
    assert (await service.get_launch_controls()).free_usage_paused is False

    now[0] += 2
    assert (await service.get_launch_controls()).free_usage_paused is True


async def test_operator_write_is_visible_at_once_in_the_writing_process(mongo_db):
    from pocketpaw_ee.cloud.launch_controls import service

    assert (await service.get_launch_controls()).free_usage_paused is False
    await _pause()
    assert (await service.get_launch_controls()).free_usage_paused is True


async def test_read_failure_fails_open_and_logs(
    ea_user,  # noqa: F811
    early_access_posture,  # noqa: F811
    monkeypatch,
    caplog,
):
    from pocketpaw_ee.cloud.credits.guards import over_billing_limit
    from pocketpaw_ee.cloud.launch_controls import service
    from pocketpaw_ee.cloud.models.launch_controls import LaunchControlsDoc

    await _pause()
    service.invalidate_cache()

    def _boom(*_a, **_k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(LaunchControlsDoc, "find_one", _boom)

    with caplog.at_level(logging.ERROR, logger=service.__name__):
        controls = await service.get_launch_controls()
    assert controls.read_failed is True
    assert controls.free_usage_paused is False
    assert controls.daily_free_ceiling_credits == 25_000
    assert any("launch_controls read failed" in r.message for r in caplog.records)

    # The row says paused, but the unreadable switch fails open: the wallet
    # gate still runs and this workspace has credits.
    assert await over_billing_limit(ea_user.workspace_id) is None


async def test_defaults_when_no_row(mongo_db):
    from pocketpaw_ee.cloud.launch_controls import service

    controls = await service.get_launch_controls()
    assert controls.signups_paused is False
    assert controls.free_usage_paused is False
    assert controls.daily_free_ceiling_credits == 25_000
    assert controls.read_failed is False


# ---------------------------------------------------------------------------
# The operator endpoint
# ---------------------------------------------------------------------------


def _request():
    from starlette.datastructures import Headers
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/api/v1/platform/launch-controls",
            "headers": Headers(raw=[(b"user-agent", b"paw-admin/test")]).raw,
            "query_string": b"",
            "client": ("10.0.0.5", 1234),
        }
    )


async def _operator():
    from pocketpaw_ee.cloud.models.user import User

    doc = User(email="op@paw.test", hashed_password="x", platform_role="operator")
    await doc.insert()
    return doc


async def test_platform_put_writes_and_audits(mongo_db):
    from pocketpaw_ee.cloud.launch_controls import service
    from pocketpaw_ee.cloud.launch_controls.dto import LaunchControlsUpdateRequest
    from pocketpaw_ee.cloud.models.launch_controls import LaunchControlsDoc
    from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
    from pocketpaw_ee.cloud.platform import launch_controls as routes

    operator = await _operator()
    out = await routes.update_launch_controls(
        LaunchControlsUpdateRequest(free_usage_paused=True, reason="cost spike"),
        request=_request(),
        operator=operator,
    )

    assert out.free_usage_paused is True
    assert out.signups_paused is False
    row = await LaunchControlsDoc.find_one(LaunchControlsDoc.key == "global")
    assert row.free_usage_paused is True and row.updated_by == str(operator.id)
    assert (await service.get_launch_controls()).free_usage_paused is True

    events = await PlatformAuditEvent.find_all().to_list()
    assert len(events) == 1
    assert events[0].action == "platform.launch_controls.write"
    assert events[0].status == "applied"
    assert events[0].before == {"free_usage_paused": False}
    assert events[0].after == {"free_usage_paused": True}

    got = await routes.get_launch_controls(_operator=operator)
    assert got.free_usage_paused is True


async def test_platform_put_requires_reason_and_a_change(mongo_db):
    from pocketpaw_ee.cloud._core.errors import ValidationError
    from pocketpaw_ee.cloud.launch_controls.dto import LaunchControlsUpdateRequest
    from pocketpaw_ee.cloud.platform import launch_controls as routes

    operator = await _operator()
    with pytest.raises(ValidationError) as no_reason:
        await routes.update_launch_controls(
            LaunchControlsUpdateRequest(free_usage_paused=True),
            request=_request(),
            operator=operator,
        )
    assert no_reason.value.code == "platform.launch_controls.reason_required"

    with pytest.raises(ValidationError) as no_change:
        await routes.update_launch_controls(
            LaunchControlsUpdateRequest(reason="nothing"), request=_request(), operator=operator
        )
    assert no_change.value.code == "platform.launch_controls.no_changes"


def _platform_actions(route) -> set[str]:
    return {
        d.call.__platform_action__
        for d in route.dependant.dependencies
        if hasattr(d.call, "__platform_action__")
    }


async def test_platform_routes_are_guarded_by_the_settings_actions():
    from pocketpaw_ee.cloud.platform.router import router

    guards = {
        method: _platform_actions(r)
        for r in router.routes
        if r.path == "/platform/launch-controls"
        for method in r.methods
    }
    assert guards == {
        "GET": {"platform.settings.read"},
        "PUT": {"platform.settings.write"},
    }
