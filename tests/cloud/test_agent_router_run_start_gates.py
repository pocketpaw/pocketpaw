"""The chat run-start checks run concurrently and keep their error precedence.

``agent_router._run_start_gates`` runs the credit balance, monthly quota, guest
turn gate and daily turn ceiling together, then raises the earliest failure in
that declared order, the order they ran in as sequential awaits. A turn that
fails several checks must get the same error it always did.
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
        if barrier is not None:
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

    async def _over_cap(_ws):
        await _step("turns")
        return "turns" in fail

    monkeypatch.setattr(mod, "get_settings", lambda: SimpleNamespace(billing_enforced=billing))
    monkeypatch.setattr("pocketpaw_ee.cloud.credits.service.check_balance", _balance)
    monkeypatch.setattr("pocketpaw_ee.cloud.credits.service.check_quota", _quota)
    monkeypatch.setattr("pocketpaw_ee.cloud.auth.guest_gates.assert_guest_turn_allowed", _guest)
    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.turn_budget.is_over_cap", _over_cap)
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
    """Every check waits on a four-party barrier, which only opens once all four
    are in flight. Sequential awaits would never get there.

    Mutation: replace the gather with a loop of ``await c`` per check.
    """
    _install(monkeypatch, billing=True, fail=set(), barrier=asyncio.Barrier(4))

    await asyncio.wait_for(mod._run_start_gates("w1", "u1", "w1"), timeout=2)
