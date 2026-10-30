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
        lambda: SimpleNamespace(
            billing_enforced=billing,
            credits_enforced=False,
            pydantic_ai_model="litellm:claude-haiku-4-5",
            free_model_allowlist=["claude-haiku-4-5"],
        ),
    )
    monkeypatch.setattr("pocketpaw_ee.cloud.credits.service.check_balance", _balance)
    monkeypatch.setattr("pocketpaw_ee.cloud.credits.service.check_quota", _quota)
    monkeypatch.setattr("pocketpaw_ee.cloud.auth.guest_gates.assert_guest_turn_allowed", _guest)
    monkeypatch.setattr("pocketpaw_ee.cloud.metering.service.used", _used)

    async def _platform(_ws):
        from pocketpaw_ee.cloud.byok.service import TurnCredentials

        return TurnCredentials(source="platform")

    # No stored key: the billing leg gates as platform, hermetically.
    monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.resolve_turn_credentials", _platform)

    # No other active run, so the early-access one-run check reads no Mongo.
    async def _no_runs(**_k):
        return []

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.runs.service.find_active_runs_for_workspace", _no_runs
    )
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

    async def _over(ws, source="platform", *, one_active_run=False, run_id=None):
        asked.append((ws, source, one_active_run, run_id))
        return InsufficientCredits(requested=1, available=0)

    monkeypatch.setattr("pocketpaw_ee.cloud.credits.guards.over_billing_limit", _over)

    with pytest.raises(InsufficientCredits) as caught:
        await mod._run_start_gates("w1", "u1", "w1")
    # The chat route asks for the early-access one-run check; no run doc exists
    # yet, so there is no run id to exclude.
    assert asked == [("w1", "platform", True, None)]
    assert caught.value.code == "credits.insufficient"


async def test_an_empty_workspace_skips_billing_like_the_guard(monkeypatch):
    """The guard returns None for an empty workspace (no wallet to attribute);
    the old inline copy called the credit service with ``""``. The route keeps
    the guard's behaviour. Unreachable live: the route rejects an empty
    workspace before it gets here."""
    started = _install(monkeypatch, billing=True, fail={"balance", "quota"})
    await mod._run_start_gates("", "u1", "")
    assert "balance" not in started and "quota" not in started


async def _byok(_ws):
    from pocketpaw_ee.cloud.byok.service import TurnCredentials

    return TurnCredentials(source="byok", api_key="k", provider="anthropic")


async def test_a_byok_turn_skips_the_wallet(monkeypatch):
    """The billing leg resolves who pays first and hands the answer to the
    guard, so a turn its own key pays for is not refused for an empty wallet.
    A deny-by-default surface is served by pydantic_ai, which spends the key,
    so no agent lookup is needed.

    Mutation: call the guard without the resolved source.
    """
    started = _install(monkeypatch, billing=True, fail={"balance", "quota"})
    monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.resolve_turn_credentials", _byok)
    await mod._run_start_gates("w1", "u1", "w1", exclusive_tools=True)
    assert "balance" not in started and "quota" not in started


async def test_a_byok_answer_without_a_spending_backend_is_gated(monkeypatch):
    """A stored key alone does not skip the wallet: with no agent whose backend
    spends it, the turn runs on our credentials and is gated as platform.

    Mutation: gate on ``resolve_payer``'s source instead of ``billing_source``.
    """
    started = _install(monkeypatch, billing=True, fail={"balance"})
    monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.resolve_turn_credentials", _byok)
    with pytest.raises(_Balance):
        await mod._run_start_gates("w1", "u1", "w1", agent_id=None)
    assert "balance" in started


async def test_billing_off_skips_the_payer_lookup(monkeypatch):
    """With both billing flags off there is nothing to gate, so the route does
    not read, decrypt or egress-check a stored key on every chat POST.

    Mutation: resolve the payer before checking the flags.
    """
    _install(monkeypatch, billing=False, fail=set())
    resolved: list[str] = []

    async def _spy(ws):
        resolved.append(ws)
        return await _byok(ws)

    monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.resolve_turn_credentials", _spy)
    await mod._run_start_gates("w1", "u1", "w1", agent_id="a1")
    assert resolved == []


async def test_only_a_deny_by_default_surface_is_exclusive():
    """The route's exclusivity answer comes from the surface's base profile.
    Mutation: return False unconditionally (the concierge would be gated on
    the bound agent's backend while pydantic_ai serves it on the key)."""
    from pocketpaw_ee.cloud.surface import SurfaceKind, SurfaceMeta, resolve_profile

    exclusive = [k for k in SurfaceKind if resolve_profile(k, SurfaceMeta()).exclusive_tools]
    assert exclusive, "no deny-by-default surface left to test"
    ctx = SimpleNamespace(kind=exclusive[0], meta=SurfaceMeta())
    assert mod._surface_is_exclusive(ctx) is True
    generic = SimpleNamespace(kind=SurfaceKind.GENERIC, meta=SurfaceMeta())
    assert mod._surface_is_exclusive(generic) is False
    assert mod._surface_is_exclusive(None) is False


async def test_a_rejected_gateway_address_is_refused_not_gated_as_platform(monkeypatch):
    """A stored gateway address the egress guard rejects refuses the turn with
    its own 422, rather than degrading to platform and spending our key or
    showing a wallet refusal.

    Mutation: let ``resolve_payer`` swallow ``GatewayEgressRejected``.
    """
    from pocketpaw_ee.cloud.byok.service import GatewayEgressRejected

    _install(monkeypatch, billing=True, fail=set())

    async def _rejected(_ws):
        raise GatewayEgressRejected("That gateway address is not allowed.")

    monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.resolve_turn_credentials", _rejected)
    with pytest.raises(GatewayEgressRejected) as caught:
        await mod._run_start_gates("w1", "u1", "w1")
    assert caught.value.code == "byok.base_url_rejected"


async def test_turn_credentials_never_print_the_key():
    """``ctx.turn_credentials`` lives on the run context, so its repr must not
    carry the plaintext key. Mutation: drop ``repr=False`` on ``api_key``."""
    from pocketpaw_ee.cloud.byok.service import TurnCredentials

    creds = TurnCredentials(source="byok", api_key="sk-secret-value", provider="anthropic")
    assert "sk-secret-value" not in repr(creds)
