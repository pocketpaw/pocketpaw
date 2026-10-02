"""The chat run-start checks run concurrently and keep their error precedence.

``agent_router._run_start_gates`` runs the credit balance, monthly quota, guest
turn gate and daily turn ceiling together, then raises the earliest failure in
that declared order, the order they ran in as sequential awaits. A turn that
fails several checks must get the same error it always did.

Updated 2026-10-01 (CN-3): the balance + quota pair is now ONE coroutine,
``credits.guards.assert_within_billing`` (the gate run_core already uses), so
those two run in sequence inside it and concurrently with the guest and turn
checks. ``billing_enforced`` is read by the guard, the turn ceiling reads the
shared ``metering`` counter, and two new tests pin the delegation and the
guard's empty-workspace behaviour.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud.chat import agent_router as mod

pytestmark = pytest.mark.asyncio


class _Balance(Exception):
    pass


class _Quota(Exception):
    pass


class _Guest(Exception):
    pass


def _install(monkeypatch, *, billing: bool, fail: set[str], barrier: asyncio.Barrier | None = None):
    """Stub the four checks. Each named in ``fail`` raises its own error."""
    started: list[str] = []

    async def _step(name: str) -> None:
        started.append(name)
        # Quota runs after balance inside the one billing coroutine, so it is
        # not a separate party to the overlap barrier.
        if barrier is not None and name != "quota":
            await barrier.wait()

    async def _balance(_ws):
        await _step("balance")
        if "balance" in fail:
            raise _Balance

    async def _quota(_ws):
        await _step("quota")
        if "quota" in fail:
            raise _Quota

    async def _guest(_uid, _ws):
        await _step("guest")
        if "guest" in fail:
            raise _Guest

    async def _used(**_k):
        await _step("turns")
        return 10**9 if "turns" in fail else 0

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.credits.guards.get_settings",
        lambda: SimpleNamespace(billing_enforced=billing),
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.credits.service.check_balance", _balance)
    monkeypatch.setattr("pocketpaw_ee.cloud.credits.service.check_quota", _quota)
    monkeypatch.setattr("pocketpaw_ee.cloud.auth.guest_gates.assert_guest_turn_allowed", _guest)
    monkeypatch.setattr("pocketpaw_ee.cloud.metering.service.used", _used)
    return started


@pytest.mark.parametrize(
    ("fail", "expected"),
    [
        ({"balance", "quota", "guest", "turns"}, _Balance),
        ({"quota", "guest", "turns"}, _Quota),
        ({"guest", "turns"}, _Guest),
        ({"balance", "guest"}, _Balance),
        ({"quota", "turns"}, _Quota),
    ],
)
async def test_the_earliest_declared_failure_wins(monkeypatch, fail, expected):
    """Mutation: raise the first exception in COMPLETION order, or reverse the
    list. The later check's error then reaches the client instead."""
    _install(monkeypatch, billing=True, fail=fail)

    with pytest.raises(expected):
        await mod._run_start_gates("w1", "u1", "w1")


async def test_guest_gate_beats_the_turn_ceiling_with_billing_off(monkeypatch):
    from pocketpaw_ee.cloud._core.errors import DailyTurnLimitError

    started = _install(monkeypatch, billing=False, fail={"guest", "turns"})
    with pytest.raises(_Guest):
        await mod._run_start_gates("w1", "u1", "w1")
    assert "balance" not in started and "quota" not in started

    _install(monkeypatch, billing=False, fail={"turns"})
    with pytest.raises(DailyTurnLimitError):
        await mod._run_start_gates("w1", "u1", "w1")


async def test_all_pass_is_a_no_op(monkeypatch):
    started = _install(monkeypatch, billing=True, fail=set())
    await mod._run_start_gates("w1", "u1", "w1")
    assert sorted(started) == ["balance", "guest", "quota", "turns"]


async def test_the_checks_overlap_instead_of_running_in_sequence(monkeypatch):
    """The billing, guest and turn checks wait on a three-party barrier, which
    only opens once all three are in flight. Sequential awaits would never get
    there.

    Mutation: replace the gather with a loop of ``await c`` per check.
    """
    _install(monkeypatch, billing=True, fail=set(), barrier=asyncio.Barrier(3))

    await asyncio.wait_for(mod._run_start_gates("w1", "u1", "w1"), timeout=2)


async def test_the_billing_leg_is_the_shared_guard(monkeypatch):
    """The route asks ``credits.guards`` — the same gate run_core uses — so a
    change to the guard cannot miss the HTTP leg. Its 402 reaches the caller
    unchanged.

    Mutation: inline ``check_balance`` / ``check_quota`` again.
    """
    from pocketpaw_ee.cloud._core.errors import InsufficientCredits

    _install(monkeypatch, billing=True, fail=set())
    asked: list[str] = []

    async def _over(ws):
        asked.append(ws)
        return InsufficientCredits(requested=1, available=0)

    monkeypatch.setattr("pocketpaw_ee.cloud.credits.guards.over_billing_limit", _over)

    with pytest.raises(InsufficientCredits) as caught:
        await mod._run_start_gates("w1", "u1", "w1")
    assert asked == ["w1"]
    assert caught.value.code == "credits.insufficient"


async def test_an_empty_workspace_skips_billing_like_the_guard(monkeypatch):
    """The guard returns None for an empty workspace (no wallet to attribute);
    the old inline copy called the credit service with ``""``. The route keeps
    the guard's behaviour. Unreachable live: the route rejects an empty
    workspace before it gets here."""
    started = _install(monkeypatch, billing=True, fail={"balance", "quota"})
    await mod._run_start_gates("", "u1", "")
    assert "balance" not in started and "quota" not in started
