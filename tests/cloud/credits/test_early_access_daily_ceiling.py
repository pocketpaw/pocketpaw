# tests/cloud/credits/test_early_access_daily_ceiling.py: the global daily free
# ceiling. Every platform-funded run on an unpaid early-access workspace reserves
# a per-run hold (``free_run_reserve_credits``, or its smaller wallet) on one
# platform meter (``DailyMeter.EARLY_ACCESS_CREDITS``, subject
# ``("workspace", "__platform__")``) before the model is called. A reservation
# past ``launch_controls.daily_free_ceiling_credits`` refuses the run with 503
# ``early_access.paused``, whichever workspace it comes from. ``bill_run``
# reconciles the reservation to the real spend (and so releases a run refused
# before the model call), exactly once. An unreadable control row refuses free
# runs (fail closed). Paid and BYOK runs never touch the meter.
#
# Same harness as ``test_early_access_run_bounds.py``: early-access posture,
# real ledger, real guard, real launch controls and daily meter on the
# in-memory Mongo, real ChatRunDoc rows. Only scope resolution, the Redis
# stream and the agent event source are stubbed.
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from tests.cloud.credits.test_early_access_byok import (
    _USAGE,
    _agent,
    _spec,
    _store_key,
    _terminal,
    _wire_executor,
)
from tests.cloud.credits.test_early_access_cap import GRANT, PRICED_MODEL, _rate_card
from tests.cloud.credits.test_early_access_run_bounds import _set_plan

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _fresh_cache():
    """The launch-controls snapshot is per process; never read another test's."""
    from pocketpaw_ee.cloud.launch_controls import service

    service.invalidate_cache()
    yield
    service.invalidate_cache()


@pytest.fixture(autouse=True)
def _pinned_rate_card(monkeypatch):
    from pocketpaw_ee.cloud.metering import service as metering

    monkeypatch.setattr(metering, "load_rate_card", _rate_card)


async def _ceiling(credits: int) -> None:
    from pocketpaw_ee.cloud.launch_controls import service

    await service.update_launch_controls(
        {"daily_free_ceiling_credits": credits}, operator_id="op-test"
    )


async def _used() -> int:
    from pocketpaw_ee.cloud.metering import service as metering

    return await metering.early_access_credits_used_today()


async def _another_user() -> SimpleNamespace:
    """A second verified owner of a free workspace holding ``GRANT`` credits,
    built the way ``ea_user`` builds the first."""
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership
    from pocketpaw_ee.cloud.models.workspace import Workspace

    tag = uuid.uuid4().hex[:8]
    user = User(email=f"ea-{tag}@example.com", hashed_password="x", is_verified=True)
    await user.insert()
    uid = str(user.id)
    ws = Workspace(name=f"EA {tag}", slug=f"ea-{tag}", owner=uid, plan="free")
    await ws.insert()
    user.active_workspace = str(ws.id)
    user.workspaces = [WorkspaceMembership(workspace=str(ws.id), role="owner")]
    await user.save()
    await credits_service.grant(
        str(ws.id),
        GRANT,
        cause="early_access",
        idempotency_key=f"early_access:{uid}",
        kind="genesis",
    )
    return SimpleNamespace(user_id=uid, workspace_id=str(ws.id))


async def _run(monkeypatch, who: SimpleNamespace, run_id: str):
    """Drive one real ``execute_run`` for ``who``; return (terminal event, seen ctxs)."""
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    ws, uid = who.workspace_id, who.user_id
    transport, seen = _wire_executor(monkeypatch, ws, uid, await _agent(ws, uid))
    spec = _spec(ws, uid, run_id)
    await run_service.create_run(spec)
    await run_core.execute_run(spec)
    return await _terminal(transport, run_id), seen


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------


async def test_daily_ceiling_refuses_with_paused_across_two_workspaces(
    ea_user, early_access_posture, backend_factory_spy, monkeypatch
):
    """Workspace A's in-flight run holds the whole ceiling, so workspace B's
    first run of the day is refused, though B's own wallet is full.

    Mutation: skip the reservation, or key the meter per workspace.
    """
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    await _ceiling(GRANT)

    end_a, seen_a = await _run(monkeypatch, ea_user, "r-a")
    assert end_a.event == "stream_end", end_a.data
    assert len(seen_a) == 1
    # A reserved its bounded estimate: the wallet it started on.
    assert await _used() == GRANT

    other = await _another_user()
    end_b, seen_b = await _run(monkeypatch, other, "r-b")
    assert end_b.event == "error"
    assert end_b.data["code"] == "early_access.paused"
    assert end_b.data["message"]
    assert seen_b == []
    assert backend_factory_spy == []
    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == "r-b")
    assert doc is not None and doc.status == "failed"
    # The refused claim was rolled back, and B holds no reservation.
    assert await _used() == GRANT
    assert doc.early_access_reserved_credits == 0


async def test_runs_pass_while_under_the_ceiling(ea_user, early_access_posture, monkeypatch):
    await _ceiling(GRANT * 2)

    end_a, _ = await _run(monkeypatch, ea_user, "r-a")
    end_b, _ = await _run(monkeypatch, await _another_user(), "r-b")

    assert end_a.event == "stream_end", end_a.data
    assert end_b.event == "stream_end", end_b.data
    assert await _used() == GRANT * 2


async def test_a_zero_ceiling_refuses_every_free_run(ea_user, early_access_posture, monkeypatch):
    await _ceiling(0)

    end, seen = await _run(monkeypatch, ea_user, "r-zero")

    assert end.event == "error" and end.data["code"] == "early_access.paused"
    assert seen == []


async def test_a_run_holds_the_per_run_reserve_not_its_whole_wallet(
    ea_user, early_access_posture, monkeypatch
):
    """Two fresh wallets fit under a ceiling of one wallet, because each run
    holds ``free_run_reserve_credits``, not the wallet it may spend: the pause
    follows spend, not how many users chat at once.

    Mutation: reserve the whole starting wallet again.
    """
    reserve = GRANT // 2 - 1
    monkeypatch.setattr(early_access_posture, "free_run_reserve_credits", reserve)
    await _ceiling(GRANT)

    end_a, _ = await _run(monkeypatch, ea_user, "r-a")
    end_b, seen_b = await _run(monkeypatch, await _another_user(), "r-b")

    assert end_a.event == "stream_end", end_a.data
    assert end_b.event == "stream_end", end_b.data
    assert len(seen_b) == 1
    assert await _used() == reserve * 2


# ---------------------------------------------------------------------------
# Reconcile and release at bill_run
# ---------------------------------------------------------------------------


async def test_bill_run_reconciles_the_reservation_down_to_actual_spend(
    ea_user, early_access_posture, monkeypatch
):
    """After A bills, the meter holds A's real spend, not its estimate, so B
    fits under a ceiling A's estimate alone would have filled.

    Mutation: never reconcile at bill_run.
    """
    from pocketpaw_ee.cloud.metering import service as metering

    end_a, _ = await _run(monkeypatch, ea_user, "r-a")
    assert end_a.event == "stream_end", end_a.data
    assert await _used() == GRANT

    result = await metering.bill_run_now("r-a")
    assert result is not None and result.debited is True
    actual = -(-result.micro_charged // 1_000_000)
    assert 0 < actual < GRANT
    assert await _used() == actual

    # A's estimate plus B's would not fit; A's actual plus B's does.
    await _ceiling(actual + GRANT)
    end_b, _ = await _run(monkeypatch, await _another_user(), "r-b")
    assert end_b.event == "stream_end", end_b.data


async def test_bill_run_charges_spend_past_the_estimate(ea_user, mongo_db):
    """A run that spent more than it reserved lands its real spend in the
    meter, so the next reservations see it.

    Mutation: only ever refund at reconcile.
    """
    from pocketpaw_ee.cloud.metering import service as metering

    reserved = 1
    doc = _doc(ea_user, "r-over", usage={**_USAGE, "output_tokens": 200_000})
    await doc.insert()
    assert await metering.reserve_early_access("r-over", reserved, ceiling_credits=100) is True
    assert await _used() == reserved

    result = await metering.bill_run(doc, rate_card=_rate_card())

    actual = -(-result.micro_charged // 1_000_000)
    assert actual > reserved
    assert await _used() == actual


async def test_a_reservation_across_utc_midnight_lands_on_the_day_it_records(
    ea_user, mongo_db, monkeypatch
):
    """The clock passes midnight inside the reservation. The meter row it
    increments and the day recorded for the refund must be the same day, or the
    refund misses and the new day stays over-counted.

    Mutation: let ``try_spend`` read the clock again.
    """
    from pocketpaw_ee.cloud.metering import service as metering
    from pocketpaw_ee.cloud.metering.domain import DailyMeter
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    clock = iter(["2026-10-15", "2026-10-16"])
    monkeypatch.setattr(metering, "today", lambda: next(clock, "2026-10-16"))
    await _doc(ea_user, "r-midnight", usage=_USAGE).insert()

    assert await metering.reserve_early_access("r-midnight", 5, ceiling_credits=100) is True

    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == "r-midnight")
    day = doc.early_access_reserved_day

    async def _on(d: str) -> int:
        return await metering.used(
            subject_type="workspace",
            subject_id=metering.PLATFORM_SUBJECT,
            meter=DailyMeter.EARLY_ACCESS_CREDITS,
            day=d,
        )

    assert await _on(day) == 5
    assert await _on("2026-10-15") + await _on("2026-10-16") == 5


async def test_a_run_refused_before_the_model_call_releases_its_reservation(
    ea_user, early_access_posture, monkeypatch
):
    """The sweeper interrupted the run while it waited, after it reserved: no
    model call, so billing it gives the whole reservation back.

    Mutation: reconcile only when the run debited.
    """
    from pocketpaw_ee.cloud.chat.runs import run_core
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.metering import service as metering

    ws, uid = ea_user.workspace_id, ea_user.user_id
    _, seen = _wire_executor(monkeypatch, ws, uid, await _agent(ws, uid))

    async def _no_longer_queued(_run_id):
        return False

    monkeypatch.setattr(run_core, "_mark_running", _no_longer_queued)
    spec = _spec(ws, uid, "r-dropped")
    await run_service.create_run(spec)
    await run_core.execute_run(spec)
    assert seen == []
    assert await _used() == GRANT

    await run_service.mark_terminal(spec.run_id, status="interrupted", error="stale")
    result = await metering.bill_run_now(spec.run_id)

    assert result is not None and result.debited is False
    assert await _used() == 0


async def test_a_second_bill_does_not_reconcile_again(ea_user, early_access_posture, monkeypatch):
    """The sweeper re-bills a run whose flag write was lost; the ledger key
    makes the debit a no-op, and the meter must not move twice either.

    Mutation: reconcile from the in-memory doc instead of claiming it once.
    """
    from pocketpaw_ee.cloud.metering import service as metering
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    end, _ = await _run(monkeypatch, ea_user, "r-twice")
    assert end.event == "stream_end", end.data
    stale = await ChatRunDoc.find_one(ChatRunDoc.run_id == "r-twice")
    assert stale.early_access_reserved_credits == GRANT

    await metering.bill_run(stale, rate_card=_rate_card())
    after_first = await _used()
    await metering.bill_run(stale, rate_card=_rate_card())

    assert after_first < GRANT
    assert await _used() == after_first


# ---------------------------------------------------------------------------
# Failure mode and scope
# ---------------------------------------------------------------------------


def _unreadable_controls(monkeypatch) -> None:
    from pocketpaw_ee.cloud.models.launch_controls import LaunchControlsDoc

    async def _boom(*_a, **_k):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(LaunchControlsDoc, "find_one", _boom)


async def test_unreadable_controls_refuse_free_runs(
    ea_user, early_access_posture, backend_factory_spy, monkeypatch
):
    """The ceiling fails closed for free runs.

    Mutation: read ``read_failed`` as "no ceiling".
    """
    _unreadable_controls(monkeypatch)

    end, seen = await _run(monkeypatch, ea_user, "r-free")

    assert end.event == "error" and end.data["code"] == "early_access.paused"
    assert seen == []
    assert backend_factory_spy == []
    assert await _used() == 0


async def test_unreadable_controls_do_not_refuse_paid_runs(
    ea_user, early_access_posture, monkeypatch
):
    """The pause flags still fail open, so a paid workspace keeps running."""
    _unreadable_controls(monkeypatch)
    await _set_plan(ea_user.workspace_id, "go")

    end, seen = await _run(monkeypatch, ea_user, "r-paid")

    assert end.event == "stream_end", end.data
    assert len(seen) == 1


@pytest.mark.parametrize("payer", ["paid_plan", "byok"])
async def test_paid_and_byok_runs_never_touch_the_meter(
    ea_user, early_access_posture, monkeypatch, payer
):
    from pocketpaw_ee.cloud.metering import service as metering
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    await _ceiling(0)  # would refuse any run that reserved
    if payer == "paid_plan":
        await _set_plan(ea_user.workspace_id, "go")
    else:
        await _store_key(monkeypatch, ea_user.workspace_id)

    end, _ = await _run(monkeypatch, ea_user, f"r-{payer}")
    assert end.event == "stream_end", end.data
    await metering.bill_run_now(f"r-{payer}")

    assert await _used() == 0
    doc = await ChatRunDoc.find_one(ChatRunDoc.run_id == f"r-{payer}")
    assert doc.early_access_reserved_credits == 0


def _doc(who: SimpleNamespace, run_id: str, *, usage: dict):
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    return ChatRunDoc(
        run_id=run_id,
        workspace=who.workspace_id,
        context_type="session",
        scope_id="s1",
        session_key="session:s1",
        user_id=who.user_id,
        agent_id="a1",
        client_message_id=f"c-{run_id}",
        user_message_id=f"m-{run_id}",
        status="completed",
        usage={**usage, "model": PRICED_MODEL},
    )
