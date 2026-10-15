# tests/cloud/credits/test_early_access_offledger.py: the spend surfaces that never
# reach the credit ledger are refused for an unpaid early-access workspace with
# 403 ``early_access.not_available``, and pass for a paid one.
#
# Surfaces covered, one test (deny) plus its paid twin (pass) each: the shared
# guard and FastAPI dependency, the Belt and Belt mandates routers, the Studio
# generation routes, Foresight scenario and backtest runs (service and chat tool
# path), the Paw Bar concierge, the media and site_media chat tools, and the
# LiveKit meeting-notes bot.
#
# Real Workspace docs and a real ledger (mongomock). The only thing replaced is
# the guard's settings source, with a copy of the real settings carrying the
# launch posture (``credits_enforced`` on, ``billing_enforced`` off).
# ruff: noqa: F811

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.credits import guards
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.workspace import Workspace

from tests.cloud.test_paw_bar_concierge_v2 import (  # noqa: F401 (fixtures)
    _HOURS_KB,
    _chat,
    _frames,
    _seed_kb,
    _site,
    _widget,
    concierge_client,
    model,
)

pytestmark = pytest.mark.asyncio

_CODE = "early_access.not_available"


def _posture(monkeypatch, **flags) -> None:
    from pocketpaw.config import get_settings

    base = {"billing_enforced": False, "credits_enforced": True, "payments_open": False}
    settings = get_settings().model_copy(update={**base, **flags})
    monkeypatch.setattr(guards, "get_settings", lambda: settings)


async def _workspace(plan: str = "free", *, exhausted: bool = True) -> str:
    """A workspace on ``plan``. Free ones hold the early-access grant, spent to
    zero when ``exhausted``; paid ones hold a positive balance."""
    tag = uuid.uuid4().hex[:8]
    ws = Workspace(name="Tenant", slug=f"t-{tag}", owner="u-owner", plan=plan)
    await ws.insert()
    wid = str(ws.id)
    await credits.grant(
        wid, 1000, cause="early_access", idempotency_key=f"early_access:{tag}", kind="genesis"
    )
    if plan == "free" and exhausted:
        await credits.debit(wid, 1000, cause="chat", idempotency_key=f"spend:{tag}")
    return wid


def _code(res) -> str | None:
    try:
        return (res.json().get("error") or {}).get("code")
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------- #
# The guard and the dependency
# --------------------------------------------------------------------------- #


async def test_unpaid_workspace_is_denied(mongo_db, monkeypatch):  # noqa: ARG001
    _posture(monkeypatch)
    ws = await _workspace("free", exhausted=False)
    exc = await guards.early_access_denial(ws)
    assert exc is not None
    assert (exc.status_code, exc.code) == (403, _CODE)


async def test_paid_workspace_is_not_denied(mongo_db, monkeypatch):  # noqa: ARG001
    _posture(monkeypatch)
    ws = await _workspace("go")
    assert await guards.early_access_denial(ws) is None


async def test_payments_open_lifts_the_deny(mongo_db, monkeypatch):  # noqa: ARG001
    _posture(monkeypatch, payments_open=True)
    ws = await _workspace("free")
    assert await guards.early_access_denial(ws) is None


async def test_deny_is_off_until_credits_are_enforced(mongo_db, monkeypatch):  # noqa: ARG001
    _posture(monkeypatch, credits_enforced=False)
    ws = await _workspace("free")
    assert await guards.early_access_denial(ws) is None


async def test_billing_enforced_alone_also_turns_the_deny_on(mongo_db, monkeypatch):  # noqa: ARG001
    _posture(monkeypatch, credits_enforced=False, billing_enforced=True)
    ws = await _workspace("free")
    assert (await guards.early_access_denial(ws)).code == _CODE


async def test_dependency_raises_for_unpaid_and_passes_paid(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud._core.deps import require_paid_or_early_access_allowed

    _posture(monkeypatch)
    free, paid = await _workspace("free"), await _workspace("go")
    with pytest.raises(CloudError) as caught:
        await require_paid_or_early_access_allowed(workspace_id=free)
    assert caught.value.code == _CODE
    assert await require_paid_or_early_access_allowed(workspace_id=paid) is None


# --------------------------------------------------------------------------- #
# Belt, Belt mandates and Studio routers (HTTP)
# --------------------------------------------------------------------------- #


def _app(router, workspace_id: str) -> FastAPI:
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    user = SimpleNamespace(
        id="u1",
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role="admin")],
    )

    async def _user():
        return user

    app.dependency_overrides[current_active_user] = _user
    return app


async def _call(app: FastAPI, method: str, path: str):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path, json={})


_BELT_CALLS = [
    ("GET", "/api/v1/belt/runs"),
    ("POST", "/api/v1/belt/repos"),
    ("POST", "/api/v1/belt/runs/a1/try"),
    ("POST", "/api/v1/belt/runs/a1/reviews"),
]
_MANDATE_CALLS = [
    ("POST", "/api/v1/belt/mandates"),
    ("POST", "/api/v1/belt/mandates/m1/plan/resolve"),
    ("POST", "/api/v1/belt/mandates/m1/shift"),
    ("POST", "/api/v1/belt/mandates/m1/autopilot"),
]
_STUDIO_CALLS = [
    ("POST", "/api/v1/studio/generate"),
    ("POST", "/api/v1/studio/edit"),
    ("POST", "/api/v1/studio/video-elements"),
    ("POST", "/api/v1/studio/video-motion-control"),
    ("POST", "/api/v1/studio/music"),
    ("POST", "/api/v1/studio/transcribe"),
]


def _router_for(path: str):
    if path.startswith("/api/v1/belt/mandates"):
        from pocketpaw_ee.cloud.mandates.router import router
    elif path.startswith("/api/v1/belt"):
        from pocketpaw_ee.cloud.belt.router import router
    else:
        from pocketpaw_ee.cloud.studio.router import router
    return router


@pytest.mark.parametrize(("method", "path"), _BELT_CALLS + _MANDATE_CALLS + _STUDIO_CALLS)
async def test_route_refuses_an_unpaid_workspace(mongo_db, monkeypatch, method, path):  # noqa: ARG001
    _posture(monkeypatch)
    ws = await _workspace("free")
    res = await _call(_app(_router_for(path), ws), method, path)
    assert res.status_code == 403, res.text
    assert _code(res) == _CODE


@pytest.mark.parametrize(("method", "path"), _BELT_CALLS + _MANDATE_CALLS + _STUDIO_CALLS)
async def test_route_lets_a_paid_workspace_through(mongo_db, monkeypatch, method, path):  # noqa: ARG001
    _posture(monkeypatch)
    ws = await _workspace("go")
    res = await _call(_app(_router_for(path), ws), method, path)
    # Whatever the route answers next (422 for the empty body, 404 for the fake
    # ids, a list), it is not the early-access refusal.
    assert _code(res) != _CODE, res.text


async def test_studio_reads_stay_open_for_an_unpaid_workspace(mongo_db, monkeypatch):  # noqa: ARG001
    """Only the generation routes spend; the gallery history still loads."""
    from pocketpaw_ee.cloud.studio import service as studio_service
    from pocketpaw_ee.cloud.studio.router import router

    _posture(monkeypatch)
    ws = await _workspace("free")
    monkeypatch.setattr(studio_service, "list_generations", AsyncMock(return_value=[]))
    res = await _call(_app(router, ws), "GET", "/api/v1/studio/generations")
    assert res.status_code == 200, res.text


# --------------------------------------------------------------------------- #
# Foresight (service, so REST and the chat tool share one check)
# --------------------------------------------------------------------------- #


def _ctx(workspace_id: str):
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    return RequestContext(
        user_id="u1",
        workspace_id=workspace_id,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


def _scenario_body():
    from pocketpaw_ee.cloud.foresight.dto import CreateScenarioRequest, PersonaSpecRequest

    return CreateScenarioRequest(
        name="ea-check",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[PersonaSpecRequest(name="Anne", role="approver", ocean={})],
    )


def _backtest_body():
    from pocketpaw_ee.cloud.foresight.dto import (
        CreateBacktestRequest,
        HistoricalAnchorRequest,
        PersonaSpecRequest,
    )

    return CreateBacktestRequest(
        name="ea-check",
        sub_type="decision_forecast",
        n_ticks=1,
        personas=[PersonaSpecRequest(name="Anne", role="approver", ocean={})],
        anchors=[
            HistoricalAnchorRequest(
                anchor_object_id="lease:LR-1", actual_outcome={"outcome": "accept"}
            )
        ],
    )


async def _foresight_runs() -> int:
    from pocketpaw_ee.cloud.foresight import service as foresight_service

    return await foresight_service._ForesightRunDoc.find_all().count()


async def test_foresight_scenario_run_refused_for_unpaid(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.foresight import service as foresight_service

    _posture(monkeypatch)
    ws = await _workspace("free")
    with pytest.raises(CloudError) as caught:
        await foresight_service.create_scenario_run(_ctx(ws), _scenario_body())
    assert caught.value.code == _CODE
    assert await _foresight_runs() == 0


async def test_foresight_scenario_run_passes_for_paid(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.foresight import service as foresight_service

    _posture(monkeypatch)
    ws = await _workspace("go")
    out = await foresight_service.create_scenario_run(_ctx(ws), _scenario_body())
    assert out.status == "complete"


async def test_foresight_backtest_refused_for_unpaid(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.foresight import service as foresight_service

    _posture(monkeypatch)
    ws = await _workspace("free")
    with pytest.raises(CloudError) as caught:
        await foresight_service.create_backtest(_ctx(ws), _backtest_body())
    assert caught.value.code == _CODE


async def test_foresight_backtest_passes_for_paid(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.foresight import service as foresight_service

    _posture(monkeypatch)
    ws = await _workspace("go")
    out = await foresight_service.create_backtest(_ctx(ws), _backtest_body())
    assert out.status == "complete"


async def test_foresight_chat_tool_relays_the_refusal(mongo_db, monkeypatch):  # noqa: ARG001
    """The chat agent's run_scenario reaches the same service check and gets
    the coded refusal back as a tool payload, not a crash."""
    from pocketpaw_ee.cloud.foresight import agent_context

    _posture(monkeypatch)
    ws = await _workspace("free")
    monkeypatch.setattr(agent_context, "_resolve_identity", lambda: (ws, "u1"))
    out = await agent_context.run_scenario_for_agent(name="x", custom_scenario_id="s1")
    assert out["ok"] is False
    assert out["error"] == _CODE


async def _insights_llm_calls(monkeypatch, plan: str) -> int:
    """How many times ``get_insights`` reached the LLM synthesizer for a
    workspace on ``plan`` with the LLM synthesizer switched on."""
    from pocketpaw_ee.cloud.foresight import service as foresight_service
    from pocketpaw_ee.cloud.foresight.dto import SetForesightInsightsConfigRequest

    _posture(monkeypatch)
    ws = await _workspace(plan)
    ctx = _ctx(ws)
    await foresight_service.set_insights_config(
        ctx, SetForesightInsightsConfigRequest(synthesizer="llm")
    )
    llm = AsyncMock(return_value=[])
    monkeypatch.setattr(foresight_service, "_synthesize_insights_llm", llm)
    await foresight_service.get_insights(ctx)
    return llm.await_count


async def test_foresight_llm_insights_off_for_unpaid(mongo_db, monkeypatch):  # noqa: ARG001
    """An unpaid workspace with the LLM synthesizer on reads pattern rows."""
    assert await _insights_llm_calls(monkeypatch, "free") == 0


async def test_foresight_llm_insights_on_for_paid(mongo_db, monkeypatch):  # noqa: ARG001
    assert await _insights_llm_calls(monkeypatch, "go") == 1


# --------------------------------------------------------------------------- #
# Paw Bar concierge (required test: exhausted unpaid workspace is refused)
# --------------------------------------------------------------------------- #

_LIMIT = ("unavailable", {"type": "unavailable", "reason": "limit"})
_END = ("stream_end", {"assistant_message_id": None, "cancelled": False})


async def _concierge_runs() -> int:
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    return await ChatRunDoc.find(ChatRunDoc.context_type == "concierge").count()


async def test_concierge_turn_on_exhausted_unpaid_workspace_is_refused(
    concierge_client, model, monkeypatch
):
    _posture(monkeypatch)
    client, store = concierge_client
    _seed_kb(monkeypatch, _HOURS_KB)
    ws = await _workspace("free")
    assert await credits.balance(ws) <= 0
    await _site(workspace=ws)
    widget = await store.create_widget(_widget(workspace_id=ws))

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert _frames(res.text) == [_LIMIT, _END]
    assert model.calls == []
    assert await _concierge_runs() == 0


async def test_concierge_turn_on_paid_workspace_is_answered(concierge_client, model, monkeypatch):
    _posture(monkeypatch)
    client, store = concierge_client
    _seed_kb(monkeypatch, _HOURS_KB)
    ws = await _workspace("go")
    await _site(workspace=ws)
    widget = await store.create_widget(_widget(workspace_id=ws))

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert _LIMIT not in _frames(res.text)
    assert model.calls != []


# --------------------------------------------------------------------------- #
# media and site_media chat tools
# --------------------------------------------------------------------------- #


def _media_handlers() -> list[tuple[str, Any, dict[str, Any]]]:
    from pocketpaw_ee.agent.mcp_servers import media, site_media

    site_args = {"prompt": "a cat", "pocket_id": "pocket-x"}
    return [
        ("media", media._image_generate_handler, {}),
        ("media", media._audio_generate_handler, {}),
        ("media", media._audio_transcribe_handler, {}),
        ("media", media._video_generate_handler, {}),
        ("site_media", site_media._generate_site_image_handler, site_args),
        ("site_media", site_media._generate_site_video_handler, site_args),
    ]


def _text_of(result: dict[str, Any]) -> str:
    return " ".join(str(c.get("text", "")) for c in result.get("content", []))


@pytest.mark.parametrize("idx", range(6))
async def test_media_tool_refused_for_unpaid(mongo_db, monkeypatch, idx):  # noqa: ARG001
    from pocketpaw_ee.agent.mcp_servers import media, site_media

    _posture(monkeypatch)
    ws = await _workspace("free")
    monkeypatch.setattr(media, "_identity", lambda: (ws, "u1"))
    monkeypatch.setattr(site_media, "_identity", lambda: (ws, "u1"))
    _, handler, args = _media_handlers()[idx]

    out = await handler(dict(args))

    assert out.get("is_error") is True
    assert "not in early access yet" in _text_of(out)


@pytest.mark.parametrize("idx", range(6))
async def test_media_tool_passes_for_paid(mongo_db, monkeypatch, idx):  # noqa: ARG001
    from pocketpaw_ee.agent.mcp_servers import media, site_media

    _posture(monkeypatch)
    ws = await _workspace("go")
    monkeypatch.setattr(media, "_identity", lambda: (ws, "u1"))
    monkeypatch.setattr(site_media, "_identity", lambda: (ws, "u1"))
    _, handler, args = _media_handlers()[idx]

    out = await handler(dict(args))

    # The handler moves on to its own checks (missing prompt, unknown site).
    assert "not in early access yet" not in _text_of(out)


# --------------------------------------------------------------------------- #
# LiveKit meeting notes
# --------------------------------------------------------------------------- #


@pytest.fixture
def _livekit(monkeypatch):
    """LiveKit stubbed at its edges; the budget is uncapped (an operator
    override) so the notes-bot decision is the only thing under test."""
    from pocketpaw_ee.cloud.livekit import service as lk

    monkeypatch.setattr(lk, "LIVEKIT_URL", "wss://test.livekit.cloud")
    monkeypatch.setattr(lk, "LIVEKIT_API_KEY", "test-key")
    monkeypatch.setattr(lk, "LIVEKIT_API_SECRET", "test-secret")
    room_svc = MagicMock()
    room_svc.list_rooms = AsyncMock(return_value=MagicMock(rooms=[MagicMock()]))
    room_svc.list_participants = AsyncMock(return_value=MagicMock(participants=[]))
    api = MagicMock()
    api.room = room_svc
    api.__aenter__ = AsyncMock(return_value=api)
    api.__aexit__ = AsyncMock(return_value=False)
    spawn = AsyncMock(return_value=MagicMock())
    lk._active_agents.clear()
    lk._spawn_locks.clear()
    with (
        patch.object(lk, "LiveKitAPI", return_value=api),
        patch.object(lk, "_spawn_agent_process", spawn),
        patch.object(lk, "_reap_agent_process", AsyncMock()),
        patch.object(lk, "_call_budget_remaining", AsyncMock(return_value=(None, 0))),
    ):
        yield spawn
    lk._active_agents.clear()
    lk._spawn_locks.clear()


async def test_livekit_notes_bot_not_spawned_for_unpaid(mongo_db, monkeypatch, _livekit):  # noqa: ARG001
    from pocketpaw_ee.cloud.livekit.service import create_room

    _posture(monkeypatch)
    ws = await _workspace("free")
    out = await create_room("g-free", ws, "u1")
    assert out["room_name"]
    _livekit.assert_not_called()


async def test_livekit_notes_bot_spawned_for_paid(mongo_db, monkeypatch, _livekit):  # noqa: ARG001
    from pocketpaw_ee.cloud.livekit.service import create_room

    _posture(monkeypatch)
    ws = await _workspace("go")
    await create_room("g-paid", ws, "u1")
    _livekit.assert_called_once()


# --------------------------------------------------------------------------- #
# Belt approve paths and the mandate service chokepoints. The routers above are
# not the only way in: the Instinct tray approves a shift plan or a Belt change,
# Mission Control goes straight to the executors, and the cadence scheduler and
# autopilot loop call the service.
# --------------------------------------------------------------------------- #


@pytest.fixture
def instinct_store(tmp_path, monkeypatch):
    from pocketpaw.instinct.store import InstinctStore

    store = InstinctStore(tmp_path / "ea.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)
    return store


async def _belt_action(store, ws: str, key: str):
    from pocketpaw.instinct.models import ActionTrigger

    if key == "_belt_plan":
        blob = {
            "kind": "belt_plan",
            "schema": 1,
            "workspace_id": ws,
            "mandate_id": "m1",
            "shift_id": "s1",
            "requested_by": "u1",
            "plan": {"tasks": []},
        }
    else:
        blob = {
            "kind": "code_change",
            "schema": 2,
            "workspace_id": ws,
            "requested_by": "u1",
            "repo": "/srv/acme",
            "base_branch": "main",
            "diff": "",
        }
    trigger = ActionTrigger(type="agent", source="belt:mandate", reason="ea")
    return await store.propose(ws, "Belt", "", "", trigger, parameters={key: blob}, workspace_id=ws)


def _instinct_app(ws: str, store) -> FastAPI:
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.instinct.router import router

    app = _app(router, ws)
    app.dependency_overrides[current_workspace_id] = lambda: ws
    return app


@pytest.fixture
def plan_executor(monkeypatch) -> list[str]:
    """The mandate plan executor, spied: a refused approve never reaches it."""
    seen: list[str] = []

    async def spy(action, *, human_event_id=None, dispatcher=None):  # noqa: ARG001
        seen.append(str(action.id))

    monkeypatch.setattr("pocketpaw_ee.cloud.mandates.executor.execute_approved_plan", spy)
    return seen


async def _approve(ws: str, store, action_id: str, *, bulk: bool):
    app = _instinct_app(ws, store)
    with patch("pocketpaw_ee.instinct.router._store", return_value=store):
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://t") as client:
            if bulk:
                return await client.post(
                    "/api/v1/instinct/actions/bulk-approve", json={"ids": [action_id]}
                )
            return await client.post(f"/api/v1/instinct/actions/{action_id}/approve")


@pytest.mark.parametrize("bulk", [False, True])
async def test_instinct_approve_refuses_an_unpaid_shift_plan(
    mongo_db, monkeypatch, instinct_store, plan_executor, bulk
):  # noqa: ARG001
    from pocketpaw.instinct.models import ActionStatus

    _posture(monkeypatch)
    ws = await _workspace("free", exhausted=False)
    action = await _belt_action(instinct_store, ws, "_belt_plan")

    res = await _approve(ws, instinct_store, action.id, bulk=bulk)

    assert res.status_code == 403, res.text
    assert _code(res) == _CODE
    assert plan_executor == []
    assert (await instinct_store.get_action(action.id)).status == ActionStatus.PENDING


@pytest.mark.parametrize("bulk", [False, True])
async def test_instinct_approve_runs_a_paid_shift_plan(
    mongo_db, monkeypatch, instinct_store, plan_executor, bulk
):  # noqa: ARG001
    _posture(monkeypatch)
    ws = await _workspace("go")
    action = await _belt_action(instinct_store, ws, "_belt_plan")

    res = await _approve(ws, instinct_store, action.id, bulk=bulk)

    assert res.status_code == 200, res.text
    assert plan_executor == [action.id]


@pytest.mark.parametrize(("plan", "denied"), [("free", True), ("go", False)])
async def test_plan_executor_checks_early_access(
    mongo_db, monkeypatch, instinct_store, plan, denied
):  # noqa: ARG001
    """Mission Control approves without the instinct router; the executor
    refuses on its own and fails the action with the early-access reason."""
    from pocketpaw_ee.cloud.mandates import executor as mandate_executor
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    _posture(monkeypatch)
    ws = await _workspace(plan)
    action = await _belt_action(instinct_store, ws, "_belt_plan")
    revalidate = AsyncMock(return_value={"exists": False})
    monkeypatch.setattr(mandate_service, "executor_revalidate", revalidate)

    await mandate_executor.execute_approved_plan(action)

    failed = await instinct_store.get_action(action.id)
    assert ("not in early access" in (failed.error or "")) is denied
    assert revalidate.await_count == (0 if denied else 1)


@pytest.mark.parametrize(("plan", "denied"), [("free", True), ("go", False)])
async def test_belt_executor_checks_early_access(
    mongo_db, monkeypatch, instinct_store, plan, denied
):  # noqa: ARG001
    from pocketpaw_ee.cloud.belt import executor as belt_executor

    _posture(monkeypatch)
    ws = await _workspace(plan)
    action = await _belt_action(instinct_store, ws, "_code_change")
    opener = MagicMock()

    await belt_executor.execute_approved_change(action, pr_opener=opener)

    failed = await instinct_store.get_action(action.id)
    # The paid twin passes the gate and fails on its empty diff instead.
    assert ("not in early access" in (failed.error or "")) is denied
    assert failed.error
    opener.assert_not_called()


async def test_trigger_shift_refuses_an_unpaid_workspace(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.mandates import service as mandate_service

    _posture(monkeypatch)
    free, paid = await _workspace("free"), await _workspace("go")
    with pytest.raises(CloudError) as caught:
        await mandate_service.trigger_shift(free, "u1", "m-missing")
    assert caught.value.code == _CODE
    # The paid workspace gets past the deny and stops on the missing mandate.
    with pytest.raises(CloudError) as caught:
        await mandate_service.trigger_shift(paid, "u1", "m-missing")
    assert caught.value.code != _CODE


@pytest.mark.parametrize(("plan", "calls"), [("free", 0), ("go", 1)])
async def test_autopilot_cycle_skips_an_unpaid_workspace(mongo_db, monkeypatch, plan, calls):  # noqa: ARG001
    from pocketpaw_ee.cloud.mandates.autopilot import run_autopilot_cycle

    _posture(monkeypatch)
    ws = await _workspace(plan)
    sim = SimpleNamespace(react=AsyncMock(return_value=[]))

    filed = await run_autopilot_cycle(ws, "m1", users=1, user_sim=sim)

    assert filed == 0
    assert sim.react.await_count == calls
