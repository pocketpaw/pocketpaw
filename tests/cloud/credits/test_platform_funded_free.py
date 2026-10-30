# tests/cloud/credits/test_platform_funded_free.py: unit tests for the
# early-access "who is unpaid" helper and the ``credits_enforced`` switch on the
# shared run-start guard. Real Workspace docs and a real ledger (mongomock); the
# only thing replaced is the guard's settings source, with a copy of the real
# settings carrying the posture under test.
from __future__ import annotations

import uuid

import pytest
from pocketpaw_ee.cloud._core.errors import InsufficientCredits
from pocketpaw_ee.cloud.credits import guards
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.workspace import Workspace

pytestmark = pytest.mark.asyncio


def _posture(monkeypatch, **flags) -> None:
    from pocketpaw.config import get_settings

    base = {"billing_enforced": False, "credits_enforced": False, "payments_open": False}
    settings = get_settings().model_copy(update={**base, **flags})
    monkeypatch.setattr(guards, "get_settings", lambda: settings)


async def _workspace(plan: str = "free") -> str:
    tag = uuid.uuid4().hex[:8]
    ws = Workspace(name="Tenant", slug=f"t-{tag}", owner="u-owner", plan=plan)
    await ws.insert()
    return str(ws.id)


async def test_free_plan_with_only_early_access_grant_is_platform_funded(mongo_db, monkeypatch):  # noqa: ARG001
    _posture(monkeypatch)
    ws = await _workspace("free")
    await credits.grant(
        ws, 1000, cause="early_access", idempotency_key="early_access:u1", kind="genesis"
    )
    assert await guards.is_platform_funded_free(ws) is True


async def test_paid_plan_is_not_platform_funded(mongo_db, monkeypatch):  # noqa: ARG001
    _posture(monkeypatch)
    ws = await _workspace("go")
    assert await guards.is_platform_funded_free(ws) is False


async def test_payments_open_means_not_platform_funded(mongo_db, monkeypatch):  # noqa: ARG001
    _posture(monkeypatch, payments_open=True)
    ws = await _workspace("free")
    assert await guards.is_platform_funded_free(ws) is False


@pytest.mark.parametrize("cause", ["top_up", "subscription_grant"])
async def test_paid_ledger_entry_means_not_platform_funded(mongo_db, monkeypatch, cause):  # noqa: ARG001
    _posture(monkeypatch)
    ws = await _workspace("free")
    await credits.grant(ws, 500, cause=cause, idempotency_key=f"paid-{cause}")
    assert await guards.is_platform_funded_free(ws) is False


async def test_unapplied_paid_entry_does_not_count(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry

    _posture(monkeypatch)
    ws = await _workspace("free")
    # A crash-window row: inserted, balance never moved. Not money received.
    await CreditLedgerEntry(
        workspace=ws,
        kind="grant",
        amount_delta_micro=1_000_000,
        balance_after_micro=0,
        applied=False,
        conditional=False,
        cause="top_up",
        idempotency_key="pending-topup",
    ).insert()
    assert await guards.is_platform_funded_free(ws) is True


async def test_unknown_workspace_counts_as_unpaid(mongo_db, monkeypatch):  # noqa: ARG001
    _posture(monkeypatch)
    assert await guards.is_platform_funded_free("not-a-real-workspace") is True


async def test_credits_enforced_alone_turns_on_the_gate(mongo_db, monkeypatch):  # noqa: ARG001
    _posture(monkeypatch, credits_enforced=True)
    ws = await _workspace("free")
    exc = await guards.over_billing_limit(ws)
    assert isinstance(exc, InsufficientCredits)
    assert exc.code == "credits.insufficient"


async def test_both_flags_off_leaves_the_gate_off(mongo_db, monkeypatch):  # noqa: ARG001
    _posture(monkeypatch)
    ws = await _workspace("free")
    assert await guards.over_billing_limit(ws) is None
