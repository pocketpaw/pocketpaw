# tests/cloud/runs/test_run_core_guest_gate.py — the executor-side guest gate:
# the SINGLE atomic spend against the guest daily-turn budget.
#
# Created 2026-09-01 (feat/byok-guest-backend). The guest sibling of
# test_run_core_credit_quota.py, and the same shape: fakeredis transport,
# stubbed ``_iter_agent_events`` recording whether the model ever ran, REAL
# gate + REAL budget rows against mongomock (the gate's fail-closed and
# atomicity are the things under test — stubbing them would test nothing).
#
# The refusal fixtures EXCEED the cap inside the test (cap 2 -> two real
# spends land first): a gate test that never crosses the line is how a
# switched-off gate ships green.

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import pytest
from pocketpaw_ee.cloud.auth import guest_budget
from pocketpaw_ee.cloud.chat.runs import run_core
from pocketpaw_ee.cloud.chat.runs.domain import RunSpec
from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport
from pocketpaw_ee.cloud.models.byok_key import ByokProviderKey
from pocketpaw_ee.cloud.models.user import GuestLimits, User

pytestmark = pytest.mark.asyncio


async def _mk_guest(*, turns: int) -> User:
    doc = User(
        email=f"guest-{id(object())}@guest.invalid",
        hashed_password="x",
        is_active=True,
        is_guest=True,
        active_workspace="w1",
        guest_limits=GuestLimits(sessions=2, turns_per_day=turns),
    )
    await doc.insert()
    return doc


async def _store_key() -> None:
    await ByokProviderKey(
        workspace="w1",
        provider="anthropic",
        encrypted_key="gAAAA-fake-envelope",
        last4="zzzz",
        key_hint="sk-ant-api03",
    ).insert()


def _spec(user_id: str) -> RunSpec:
    return RunSpec(
        run_id="r1",
        workspace_id="w1",
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        group=None,
        user_id=user_id,
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


def _wire(monkeypatch, transport, *, user_id: str) -> dict[str, Any]:
    """Stub the executor's collaborators; record whether the model ran and
    every mark_terminal call. The guest gate itself is REAL."""
    called: dict[str, Any] = {"agent_loop": False, "terminal": []}

    async def tracking_agent_events(spec, ctx):
        called["agent_loop"] = True
        yield ("chunk", {"content": "hello", "type": "text"})

    async def fake_resolve_scope_context(**_):
        from types import SimpleNamespace

        return SimpleNamespace(
            kind=SimpleNamespace(value="session"),
            scope_id="s1",
            workspace_id="w1",
            user_id=user_id,
            target_agent_id="a1",
            members=[user_id],
            session_id=None,
            intent=None,
            surface_context=None,
            resolved_profile=None,
            pocket_id=None,
        )

    monkeypatch.setattr(run_core, "_iter_agent_events", tracking_agent_events)
    monkeypatch.setattr(run_core, "get_stream_transport", lambda: transport)
    monkeypatch.setattr(run_core, "_mark_running", _noop)
    monkeypatch.setattr(run_core, "_persist_and_complete", _persist_stub)
    monkeypatch.setattr(run_core, "_broadcast_agent_typing", _noop)
    monkeypatch.setattr(run_core, "resolve_scope_context", fake_resolve_scope_context)
    monkeypatch.setattr("pocketpaw_ee.cloud.sessions.service.ensure_for_agent_scope", _noop)
    monkeypatch.setattr(run_core, "_resolve_entity_profile", _noop)

    async def _no_jail_reject(spec, ctx, transport):
        return False

    monkeypatch.setattr(run_core, "_reject_if_over_jail_quota", _no_jail_reject)

    async def _track_terminal(run_id, *, status, error=None, **k):
        called["terminal"].append({"run_id": run_id, "status": status})

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.run_core.run_service.mark_terminal", _track_terminal
    )
    return called


async def _events(transport) -> list[Any]:
    return [e async for e in transport.read_events("r1", after="0", block_ms=10)]


# ---------------------------------------------------------------------------


async def test_a_guest_over_the_cap_is_rejected_and_the_model_never_runs(monkeypatch, mongo_db):
    guest = await _mk_guest(turns=2)
    await _store_key()
    uid = str(guest.id)
    # EXCEED the cap: two real spends first.
    assert (await guest_budget.try_spend_turn(uid, 2))[0] is True
    assert (await guest_budget.try_spend_turn(uid, 2))[0] is True

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    called = _wire(monkeypatch, transport, user_id=uid)

    await run_core.execute_run(_spec(uid))

    assert called["agent_loop"] is False, "the model must NOT run past the guest cap"
    events = await _events(transport)
    assert [e.event for e in events] == ["error"]
    assert events[0].data["code"] == "guest_limit_reached"
    assert events[0].data["kind"] == "turns"
    assert called["terminal"] == [{"run_id": "r1", "status": "failed"}]
    # The refused claim was rolled back — the counter did not run away.
    assert await guest_budget.turns_used_today(uid) == 2


async def test_a_guest_under_the_cap_runs_and_spends_exactly_one(monkeypatch, mongo_db):
    guest = await _mk_guest(turns=5)
    await _store_key()
    uid = str(guest.id)

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    called = _wire(monkeypatch, transport, user_id=uid)

    await run_core.execute_run(_spec(uid))

    assert called["agent_loop"] is True
    assert await guest_budget.turns_used_today(uid) == 1, (
        "the executor is the single spend site — exactly one per turn"
    )


async def test_a_keyless_guest_is_rejected_without_spending(monkeypatch, mongo_db):
    """No stored key -> guest_key_required, and the counter is untouched (the
    key check runs BEFORE the spend, so a re-prompted guest keeps their day)."""
    guest = await _mk_guest(turns=5)  # no _store_key()
    uid = str(guest.id)

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    called = _wire(monkeypatch, transport, user_id=uid)

    await run_core.execute_run(_spec(uid))

    assert called["agent_loop"] is False
    events = await _events(transport)
    assert [e.event for e in events] == ["error"]
    assert events[0].data["code"] == "guest_key_required"
    assert await guest_budget.turns_used_today(uid) == 0


async def test_a_non_guest_runs_with_no_counter_row(monkeypatch, mongo_db):
    doc = User(email="real@x.co", hashed_password="x", is_active=True)
    await doc.insert()
    uid = str(doc.id)

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    transport = RedisStreamTransport(redis)
    called = _wire(monkeypatch, transport, user_id=uid)

    await run_core.execute_run(_spec(uid))

    assert called["agent_loop"] is True
    assert await guest_budget.turns_used_today(uid) == 0
