# tests/cloud/runs/test_run_core_credit_quota.py
# Created 2026-06-30 (feat/billing-quota-enforcement, chunk 3) — locks the
# universal run-start MONTHLY-CREDIT-QUOTA gate in
# run_core (the credit-spend sibling of the ART-3 jail-quota reject):
#   * billing_enforced + over-quota -> the run is rejected CLEANLY (terminal
#     ``error`` stream frame + ``mark_terminal(failed)``) and the agent/model
#     (``_iter_agent_events``) is NEVER invoked (the money guarantee).
#   * billing_enforced + under-quota -> the run proceeds normally (the agent
#     loop runs, ``stream_end`` is written).
#   * flag OFF (default) -> ``check_quota`` is NEVER called — the gate never
#     fires, even when the workspace is over its ceiling.
#
# Harness mirrors test_run_core.py (fakeredis transport + a stubbed
# ``_iter_agent_events``) and test_run_core_jail_quota.py (a clean reject, not a
# crash). ``check_quota`` is stubbed at the run_core seam (it is called via the
# locally-imported ``credits_service`` module, patched on that module) so these
# tests assert the WIRING, not the chunk-2 quota math (test_quota.py owns that).
#
# Changed 2026-07-08 (feat/billing-enforce-gate): run_core's
# ``_reject_if_over_credit_quota`` now delegates to the shared
# ``credits.guards.reject_if_over_billing``, so the worker/executor leg gained the
# BALANCE assertion (``check_balance``, wallet <= 0) it lacked — it ran only
# ``check_quota`` before. Two harness updates follow: (1) the ``billing_enforced``
# flag is now read inside the guards module, so the flag stub is applied to
# ``credits.guards.get_settings`` (not run_core's); (2) ``check_balance`` runs
# BEFORE ``check_quota`` in the shared gate, so it is stubbed to a no-op by default
# in ``_wire_common`` and the quota cases stay isolated. A new case locks the new
# leg: enforced + empty wallet -> the same clean reject with no model call.

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud._core.errors import QuotaExceeded
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

pytestmark = pytest.mark.asyncio


def _spec() -> RunSpec:
    return RunSpec(
        run_id="r1",
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id="u1",
        agent_id="a1",
        client_message_id="c1",
        user_message_id="m1",
        content="hi",
        history=[],
        intent=None,
    )


async def _noop(*a, **k):
    return None


async def _persist_stub(spec, ctx, full_text, attachments, usage=None):
    return "assistant-msg-1"


async def fake_resolve_scope_context(**_):
    class _Ctx:
        kind = type("K", (), {"value": "session"})()
        scope_id = "s1"
        workspace_id = "w1"
        user_id = "u1"
        target_agent_id = "a1"
        members = ["u1"]
        session_id = None
        intent = None
        surface_context = None
        resolved_profile = None
        pocket_id = None

    return _Ctx()


def _wire_common(monkeypatch, transport, *, enforced: bool) -> dict[str, bool]:
    """Patch the side-effecting collaborators the executor calls AND record
    whether the agent loop (``_iter_agent_events``) ever ran. Returns the
    ``called`` dict so a test can assert the model was / was not invoked."""
    called: dict[str, bool] = {"agent_loop": False}

    async def tracking_agent_events(spec, ctx):
        called["agent_loop"] = True
        yield ("chunk", {"content": "hello", "type": "text"})

    monkeypatch.setattr(run_core, "_iter_agent_events", tracking_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    # The executor mirrors agent_router._ensure_scope_session via the sessions
    # service — stub it so no real Mongo is needed.
    monkeypatch.setattr("pocketpaw_ee.cloud.sessions.service.ensure_for_agent_scope", _noop)
    # Resolve the entity profile to None (the legacy/no-pocket path) without a DB.
    monkeypatch.setattr(run_core, "_resolve_entity_profile", _noop)

    # The ART-3 jail-quota gate is a no-op here (off cloud / no jail) — keep it
    # out of the way so we isolate the credit-quota gate.
    async def _no_jail_reject(spec, ctx, transport):
        return False

    monkeypatch.setattr(run_core, "_reject_if_over_jail_quota", _no_jail_reject)
    # Point the flag at a stub carrying the desired posture. The billing flag is
    # read inside the shared guard, so patch THAT module's get_settings —
    # run_core no longer imports it (the orphaned import was dropped).
    from types import SimpleNamespace

    from pocketpaw_ee.cloud.credits import guards

    stub_settings = SimpleNamespace(billing_enforced=enforced)
    monkeypatch.setattr(guards, "get_settings", lambda: stub_settings)

    # The shared gate now runs check_balance BEFORE check_quota. Default it to a
    # no-op so the quota-focused cases below stay isolated (the balance-reject
    # case overrides it). Patched on the service module the guard imports.
    async def _balance_ok(workspace_id):
        return None

    monkeypatch.setattr("pocketpaw_ee.cloud.credits.service.check_balance", _balance_ok)
    return called


# ---------------------------------------------------------------------------
# 1 — enforced + OVER quota -> run rejected, agent/model NOT called.
# ---------------------------------------------------------------------------


async def test_enforced_over_quota_rejects_and_does_not_call_model(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    called = _wire_common(monkeypatch, transport, enforced=True)

    # check_quota raises QuotaExceeded -> the gate rejects the run.
    async def _over_quota(workspace_id):
        raise QuotaExceeded(ceiling=1000, spent=1000)

    monkeypatch.setattr("pocketpaw_ee.cloud.credits.service.check_quota", _over_quota)

    mark_calls: list[dict[str, Any]] = []

    async def _track_terminal(run_id, *, status, error=None, **k):
        mark_calls.append({"run_id": run_id, "status": status, "error": error})

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _track_terminal
    )

    await run_core.execute_run(_spec())

    # THE MONEY GUARANTEE: the agent loop (model) never ran.
    assert called["agent_loop"] is False, "model must NOT be invoked on an over-quota block"

    # A terminal ``error`` frame went to the stream (mirrors the jail-quota reject).
    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert [e.event for e in events] == ["error"]
    assert events[0].data["code"] == "credits.quota_exceeded"
    # The run was marked terminally failed (and ONLY that — no completed mark).
    assert mark_calls == [{"run_id": "r1", "status": "failed", "error": events[0].data["message"]}]


# ---------------------------------------------------------------------------
# 1b — enforced + EMPTY WALLET (balance <= 0) -> run rejected, model NOT called.
#      Locks the new balance leg the worker/executor path gained by delegating to
#      the shared guard (it previously caught only the quota case).
# ---------------------------------------------------------------------------


async def test_enforced_empty_wallet_rejects_and_does_not_call_model(monkeypatch):
    from pocketpaw_ee.cloud._core.errors import InsufficientCredits

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    called = _wire_common(monkeypatch, transport, enforced=True)

    # check_balance raises FIRST (wallet <= 0) -> the gate rejects before quota.
    async def _empty_wallet(workspace_id):
        raise InsufficientCredits(1, 0)

    monkeypatch.setattr("pocketpaw_ee.cloud.credits.service.check_balance", _empty_wallet)

    # check_quota must never be reached — balance is the primary guard.
    async def _quota_unreached(workspace_id):
        raise AssertionError("check_quota must not run once check_balance rejects")

    monkeypatch.setattr("pocketpaw_ee.cloud.credits.service.check_quota", _quota_unreached)

    mark_calls: list[dict[str, Any]] = []

    async def _track_terminal(run_id, *, status, error=None, **k):
        mark_calls.append({"run_id": run_id, "status": status, "error": error})

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _track_terminal
    )

    await run_core.execute_run(_spec())

    # THE MONEY GUARANTEE: the agent loop (model) never ran.
    assert called["agent_loop"] is False, "model must NOT be invoked on an empty-wallet block"

    # A terminal ``error`` frame went to the stream with the balance code.
    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert [e.event for e in events] == ["error"]
    assert events[0].data["code"] == "credits.insufficient"
    assert mark_calls == [{"run_id": "r1", "status": "failed", "error": events[0].data["message"]}]


# ---------------------------------------------------------------------------
# 2 — enforced + UNDER quota -> proceeds normally (agent loop runs).
# ---------------------------------------------------------------------------


async def test_enforced_under_quota_proceeds(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    called = _wire_common(monkeypatch, transport, enforced=True)

    quota_calls: list[str] = []

    async def _under_quota(workspace_id):
        quota_calls.append(workspace_id)  # no-op (under ceiling)

    monkeypatch.setattr("pocketpaw_ee.cloud.credits.service.check_quota", _under_quota)

    await run_core.execute_run(_spec())

    # The gate ran (flag on) and passed, so the agent loop proceeded.
    assert quota_calls == ["w1"]
    assert called["agent_loop"] is True
    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert [e.event for e in events] == ["chunk", "stream_end"]


# ---------------------------------------------------------------------------
# 3 — flag OFF (default) -> check_quota is NEVER called; run proceeds.
# ---------------------------------------------------------------------------


async def test_not_enforced_never_calls_check_quota(monkeypatch):
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    called = _wire_common(monkeypatch, transport, enforced=False)

    quota_calls: list[str] = []

    async def _should_not_run(workspace_id):
        quota_calls.append(workspace_id)
        raise AssertionError("check_quota must NOT be called when the flag is OFF")

    monkeypatch.setattr("pocketpaw_ee.cloud.credits.service.check_quota", _should_not_run)

    await run_core.execute_run(_spec())

    assert quota_calls == [], "the flag-off path must never gate"
    assert called["agent_loop"] is True
    events = [e async for e in transport.read_events("r1", after="0", block_ms=10)]
    assert [e.event for e in events] == ["chunk", "stream_end"]
