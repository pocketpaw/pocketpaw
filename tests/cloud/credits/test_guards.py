# tests/cloud/credits/test_guards.py
# Created 2026-07-08 (feat/billing-enforce-gate) — locks the shared run-start
# BILLING gate (``credits.guards``) that every agent-run seam now funnels through.
# These are GUARD-level tests: the seam under test is the guard, so they drive the
# REAL ``check_balance`` / ``check_quota`` off real wallet + plan state (funded via
# ``credits.grant``, spend back-dated into the ledger, plan set by inserting a real
# ``Workspace``) rather than stubbing the credit assertions — over-mocking has hid
# live billing bugs here before. The credit-math contract itself lives in
# test_quota.py / test_enforcement.py; this locks the GUARD wiring:
#   * ``over_billing_limit`` — flag OFF is a no-op (None) even at balance 0; flag
#     ON returns InsufficientCredits at an empty wallet (the primary money leg,
#     checked FIRST), QuotaExceeded when funded-but-over-the-monthly-ceiling, and
#     None when funded + under the ceiling. An uncapped Enterprise plan
#     (ceiling=None) is NEVER blocked by the quota leg. An empty workspace id is a
#     no-op (no wallet to attribute).
#   * ``assert_within_billing`` — raises the 402 CloudError over-limit (the HTTP
#     shape), no-ops within limits.
#   * ``reject_if_over_billing`` — the run-transport shape: on rejection emits a
#     terminal ``error`` frame, marks the run failed, sets the stream TTL, returns
#     True; flag OFF returns False with no side effects.
# The flag is applied ON in the settings equivalent (``guards.get_settings`` stub
# with ``billing_enforced=True``) — the flag-mode lane.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud._core.errors import InsufficientCredits, QuotaExceeded
from pocketpaw_ee.cloud.credits import guards
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry
from pocketpaw_ee.cloud.models.workspace import Workspace

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers (seeding style mirrors test_quota.py)
# ---------------------------------------------------------------------------


def _enforce(monkeypatch, *, on: bool) -> None:
    """Point the guard's ``get_settings`` at a stub carrying the flag posture."""
    monkeypatch.setattr(guards, "get_settings", lambda: SimpleNamespace(billing_enforced=on))


async def _make_workspace(plan: str, *, slug: str) -> str:
    """Insert a real Workspace so ``resolve_entitlements`` reads its plan."""
    ws = Workspace(name="Tenant", slug=slug, owner="u-owner", plan=plan)
    await ws.insert()
    return str(ws.id)


async def _seed_spend(ws: str, amount: int, *, idem: str) -> None:
    """Back-date an APPLIED compute_spend debit into the current month WITHOUT
    moving the CreditBalance (raw insert) so a funded wallet can still be 'over
    quota' — isolating the quota leg from the balance leg."""
    n = datetime.now(UTC)
    when = datetime(n.year, n.month, 15, 12, tzinfo=UTC)
    entry = CreditLedgerEntry(
        workspace=ws,
        kind="spend",
        amount_delta=-amount,
        balance_after=0,
        applied=True,
        conditional=False,
        cause="compute_spend",
        ref={},
        idempotency_key=idem,
    )
    await entry.insert()
    await CreditLedgerEntry.get_pymongo_collection().update_one(
        {"_id": entry.id}, {"$set": {"createdAt": when}}
    )


# ---------------------------------------------------------------------------
# over_billing_limit
# ---------------------------------------------------------------------------


async def test_flag_off_is_noop_even_at_zero_balance(mongo_db, monkeypatch):  # noqa: ARG001
    _enforce(monkeypatch, on=False)
    # No wallet -> balance 0, but the flag is OFF: never gates.
    assert await guards.over_billing_limit("ws-empty") is None


async def test_enforced_empty_wallet_returns_insufficient(mongo_db, monkeypatch):  # noqa: ARG001
    _enforce(monkeypatch, on=True)
    # No wallet -> balance 0 -> the balance leg (checked first) rejects.
    exc = await guards.over_billing_limit("ws-empty")
    assert isinstance(exc, InsufficientCredits)
    assert exc.status_code == 402
    assert exc.code == "credits.insufficient"


async def test_enforced_funded_over_quota_returns_quota_exceeded(mongo_db, monkeypatch):  # noqa: ARG001
    _enforce(monkeypatch, on=True)
    ws = await _make_workspace("free", slug="g-over")
    # Fund the wallet so the balance leg passes, then spend up to the Free ceiling
    # (1000) so the quota leg trips. The spend is raw-seeded so it does NOT reduce
    # the funded balance — the wallet is genuinely funded AND over the monthly cap.
    await credits.grant(ws, 5000, cause="test.seed", idempotency_key="fund-1")
    await _seed_spend(ws, 1000, idem="spend-1")

    exc = await guards.over_billing_limit(ws)
    assert isinstance(exc, QuotaExceeded)
    assert exc.status_code == 402
    assert exc.code == "credits.quota_exceeded"


async def test_enforced_funded_under_quota_returns_none(mongo_db, monkeypatch):  # noqa: ARG001
    _enforce(monkeypatch, on=True)
    ws = await _make_workspace("free", slug="g-under")
    await credits.grant(ws, 5000, cause="test.seed", idempotency_key="fund-2")
    await _seed_spend(ws, 999, idem="spend-2")  # under the 1000 Free ceiling

    assert await guards.over_billing_limit(ws) is None


async def test_enforced_enterprise_uncapped_never_blocked(mongo_db, monkeypatch):  # noqa: ARG001
    _enforce(monkeypatch, on=True)
    # Enterprise -> ceiling None. Funded wallet + huge spend must NOT block: the
    # quota leg is a no-op on an uncapped plan (the guardrail).
    ws = await _make_workspace("enterprise", slug="g-ent")
    await credits.grant(ws, 5000, cause="test.seed", idempotency_key="fund-3")
    await _seed_spend(ws, 1_000_000, idem="spend-3")

    assert await guards.over_billing_limit(ws) is None


async def test_enforced_empty_workspace_id_is_noop(mongo_db, monkeypatch):  # noqa: ARG001
    _enforce(monkeypatch, on=True)
    # No workspace to attribute -> proceed (no wallet, no gate).
    assert await guards.over_billing_limit("") is None


# ---------------------------------------------------------------------------
# assert_within_billing (the HTTP shape)
# ---------------------------------------------------------------------------


async def test_assert_raises_over_limit(mongo_db, monkeypatch):  # noqa: ARG001
    _enforce(monkeypatch, on=True)
    with pytest.raises(InsufficientCredits) as exc:
        await guards.assert_within_billing("ws-empty")
    assert exc.value.status_code == 402


async def test_assert_noop_within_limit(mongo_db, monkeypatch):  # noqa: ARG001
    _enforce(monkeypatch, on=True)
    ws = await _make_workspace("free", slug="g-assert-ok")
    await credits.grant(ws, 5000, cause="test.seed", idempotency_key="fund-4")
    assert await guards.assert_within_billing(ws) is None


async def test_assert_flag_off_never_raises(mongo_db, monkeypatch):  # noqa: ARG001
    _enforce(monkeypatch, on=False)
    # Empty wallet but flag OFF -> no raise.
    assert await guards.assert_within_billing("ws-empty") is None


# ---------------------------------------------------------------------------
# reject_if_over_billing (the run/stream-transport shape)
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Records the run-transport side effects the guard drives on a rejection."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, dict]] = []
        self.ttls: list[tuple[str, int]] = []

    async def append_event(self, run_id: str, event: str, data: dict) -> None:
        self.events.append((run_id, event, data))

    async def set_ttl(self, run_id: str, ttl: int) -> None:
        self.ttls.append((run_id, ttl))


async def test_reject_over_limit_emits_terminal_and_marks_failed(mongo_db, monkeypatch):  # noqa: ARG001
    _enforce(monkeypatch, on=True)
    transport = _FakeTransport()

    marked: list[dict] = []

    async def _mark_terminal(run_id, *, status, error=None, **k):
        marked.append({"run_id": run_id, "status": status, "error": error})

    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.service.mark_terminal", _mark_terminal)

    # Empty wallet -> the balance leg rejects.
    rejected = await guards.reject_if_over_billing("ws-empty", run_id="run-1", transport=transport)

    assert rejected is True
    # A terminal ``error`` frame carrying the balance code went to the transport.
    assert transport.events == [
        ("run-1", "error", transport.events[0][2]),
    ]
    assert transport.events[0][2]["code"] == "credits.insufficient"
    # The run was marked terminally failed, and the stream TTL was set.
    assert marked == [
        {"run_id": "run-1", "status": "failed", "error": transport.events[0][2]["message"]}
    ]
    assert transport.ttls and transport.ttls[0][0] == "run-1"


async def test_reject_within_limit_returns_false_no_side_effects(mongo_db, monkeypatch):  # noqa: ARG001
    _enforce(monkeypatch, on=True)
    ws = await _make_workspace("free", slug="g-reject-ok")
    await credits.grant(ws, 5000, cause="test.seed", idempotency_key="fund-5")
    transport = _FakeTransport()

    async def _mark_terminal(run_id, *, status, error=None, **k):
        raise AssertionError("mark_terminal must not run when within budget")

    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.service.mark_terminal", _mark_terminal)

    rejected = await guards.reject_if_over_billing(ws, run_id="run-2", transport=transport)

    assert rejected is False
    assert transport.events == []
    assert transport.ttls == []


async def test_reject_flag_off_returns_false(mongo_db, monkeypatch):  # noqa: ARG001
    _enforce(monkeypatch, on=False)
    transport = _FakeTransport()

    async def _mark_terminal(run_id, *, status, error=None, **k):
        raise AssertionError("mark_terminal must not run when the flag is OFF")

    monkeypatch.setattr("pocketpaw_ee.cloud.chat.runs.service.mark_terminal", _mark_terminal)

    # Empty wallet but flag OFF -> no rejection, no side effects.
    rejected = await guards.reject_if_over_billing("ws-empty", run_id="run-3", transport=transport)
    assert rejected is False
    assert transport.events == []
    assert transport.ttls == []
