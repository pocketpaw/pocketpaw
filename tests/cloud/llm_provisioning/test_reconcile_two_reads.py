# tests/cloud/llm_provisioning/test_reconcile_two_reads.py — proves the SHADOW
# compare reads the same two halves of the proxy the live ingest reads.
#
# THE BUG. ``reconcile_tenant_spend`` is the one instrument an operator is told to
# trust before making LiteLLM the sole meter, and it read the proxy ONCE:
# ``/spend/logs?api_key=<tenant virtual key>``. The live ingest reads TWICE —
# customer-scoped (``/spend/logs/v2?end_user=<workspace>``) plus per-key — because
# the customer read is the only one that sees a chat run, chat having
# authenticated with the deployment key. The compare never got that second read.
#
# So in shadow the instrument reported, on the deployment shape recorded
# 2026-09-02 (three tenants with keys and no spend, three customers with spend and
# no keys):
#
#   * a provisioned tenant whose spend is chat -> ``litellm=0, bc3=N, delta=-N``,
#     a coverage gap manufactured out of a read it never performed;
#   * a workspace spending with no key at all -> ``litellm=0, bc3=0``, the
#     agreement of two meters that both looked away.
#
# These tests are written against that shape, not against the new code:
#
#   * spend visible on BOTH reads is SUMMED (the failing assertion before the fix);
#   * a row both reads return is counted ONCE;
#   * a workspace with spend and NO tenant key no longer reconciles to zero;
#   * the compare bounds the customer read by the CALLER's window, not by the
#     ingest high-water mark (shadow must neither honour nor advance it);
#   * and the critical invariant survives the second read: shadow still debits
#     nothing, and still creates no provisioning row.
#
# ONE WINDOW, BOTH METERS. The bug has a general form the second read alone does
# not close: the two halves resolving their span separately. Wiring the proxy read
# up without that produced it again on the windowless path — the LiteLLM side
# derived a start from the tenant's own row (zero-width on a freshly provisioned
# tenant, 15 minutes on a swept one) while the ledger side summed all time, which
# is a coverage gap nothing spent. So three tests pin the window itself:
#
#   * no window at all -> both sides measure the same span, and the audit row
#     reports the span actually compared rather than a null;
#   * an open UPPER bound -> both sides stop at the same instant, proven with a
#     row and a debit stamped past it;
#   * an inverted window -> refused, rather than recorded as a vacuous agreement.
#
# Mutation-proven: tests/mutations/reconcile_two_reads.json breaks the per-key-only
# read, both halves of the window resolution, the recorded bounds, the inverted-
# window guard, and the read-only discipline. All six were observed to fail here.
#
# Uses the shared ``mongo_db`` + autouse ``recording_bus`` fixtures from
# tests/cloud/conftest.py. A FAKE admin client stands in for the proxy.
#
# Created 2026-09-11 (fix/billing-reconcile-two-reads): new test module.
# Updated 2026-09-11 (same branch, review): added the three window tests + the
#   mutation plan, after review found the windowless path reproducing the very
#   false gap this module exists to prevent.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.catalog.admin_client import LiteLLMAdminError
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning
from pocketpaw_ee.cloud.llm_provisioning.domain import KeyBudget, SpendCredits
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey
from pocketpaw_ee.cloud.models.spend_reconciliation import SpendReconciliation

WS = "ws_reconcile_two_reads"

# Pinned so credits never depend on ambient settings: round(usd * 250).
SPEND = SpendCredits(markup=2.5, credit_usd=0.01)

# The window every test reconciles over. Wide enough to hold the rows below, and
# explicit so the compare never depends on wall-clock or on a high-water mark.
SINCE = datetime(2026, 9, 1, tzinfo=UTC)
UNTIL = datetime(2026, 9, 3, tzinfo=UTC)


class FakeAdmin:
    """In-memory stand-in for LiteLLMAdminClient, with BOTH spend reads.

    ``key_rows`` is what ``/spend/logs?api_key=`` returns — what a tenant's own
    virtual key accrued. ``customer_rows`` is what ``/spend/logs/v2?end_user=``
    returns — every row tagged with the workspace, whichever key paid. A chat row
    appears only in the second. ``windows`` records the window the customer read
    was asked for, so a test can prove where that bound came from.
    """

    def __init__(
        self,
        *,
        key_rows: list[dict] | None = None,
        customer_rows: list[dict] | None = None,
        fail_customer_read: bool = False,
        fail_key_read: bool = False,
    ) -> None:
        self.key_rows = key_rows or []
        self.customer_rows = customer_rows or []
        self.fail_customer_read = fail_customer_read
        self.fail_key_read = fail_key_read
        self.windows: list[tuple[str, str]] = []
        self.key_reads: list[str] = []

    async def generate_key(self, **kwargs):
        return {"key": f"sk-{kwargs.get('key_alias', 'x')}", **kwargs}

    async def spend_logs(self, *, api_key: str):
        self.key_reads.append(api_key)
        if self.fail_key_read:
            raise LiteLLMAdminError("per-key read failed (simulated)")
        return list(self.key_rows)

    async def spend_logs_by_end_user(self, *, end_user, start_date, end_date, page_size=100):
        self.windows.append((start_date, end_date))
        if self.fail_customer_read:
            raise LiteLLMAdminError("customer read failed (simulated)")
        return list(self.customer_rows)


def _row(rid: str, *, usd: float, at: str = "2026-09-02T10:00:00") -> dict:
    return {"request_id": rid, "spend": usd, "startTime": at, "model": "gpt-5.2"}


async def _ledger_snapshot(workspace: str) -> tuple[int, int]:
    """(#ledger entries, balance) — the two things shadow must NOT change."""
    entries = await CreditLedgerEntry.find(CreditLedgerEntry.workspace == workspace).to_list()
    return len(entries), await credits.balance(workspace)


async def _provision(workspace: str = WS) -> None:
    await provisioning.ensure_tenant_key(workspace, budget=KeyBudget(), admin_client=FakeAdmin())


# ===========================================================================
# The bug: half the proxy.
# ===========================================================================


async def test_reconcile_sums_both_reads(mongo_db):
    """The whole point. A chat row rides the deployment key and appears ONLY in
    the customer-scoped read; a Studio row the tenant key paid for appears in the
    per-key read. The compare must count both, or it invents a gap."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(
        key_rows=[_row("req-key", usd=0.04)],  # 10 credits
        customer_rows=[_row("req-chat", usd=0.08)],  # 20 credits
    )

    rec = await provisioning.reconcile_tenant_spend(
        WS, since=SINCE, until=UNTIL, spend_card=SPEND, threshold=2, admin_client=admin
    )

    assert rec.litellm_rows == 2, "the customer-scoped read was never performed"
    assert rec.litellm_credits == 30  # round((0.04 + 0.08) * 250)


async def test_reconcile_counts_a_row_both_reads_return_once(mongo_db):
    """Studio and the media server send the tenant key AND tag ``user``, so their
    rows come back from both reads. Merging on ``request_id`` keeps the compare
    from double-counting them into a gap that is not there."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    shared = _row("req-both", usd=0.04)
    admin = FakeAdmin(key_rows=[shared], customer_rows=[dict(shared)])

    rec = await provisioning.reconcile_tenant_spend(
        WS, since=SINCE, until=UNTIL, spend_card=SPEND, threshold=2, admin_client=admin
    )

    assert rec.litellm_rows == 1, "the same row was counted twice"
    assert rec.litellm_credits == 10


async def test_reconcile_sees_a_workspace_with_spend_and_no_key(mongo_db):
    """The production shape. Three customers had spend and no key; every one of
    them reconciled to ``litellm=0, bc3=0`` — two meters agreeing because both
    looked away. No provisioning row exists here at all."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await credits.debit(
        WS, 20, cause="compute_spend", idempotency_key="run:r1", allow_negative=True
    )
    assert await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == WS) is None

    admin = FakeAdmin(customer_rows=[_row("req-chat", usd=0.08)])  # 20 credits

    # Open-ended at the top so the BC-3 debit, whose ``createdAt`` is wall-clock
    # now, lands in the same window as the pinned proxy row — this test is about
    # the two meters agreeing, so both sides have to be inside the compare.
    rec = await provisioning.reconcile_tenant_spend(
        WS,
        since=SINCE,
        until=datetime(2999, 1, 1, tzinfo=UTC),
        spend_card=SPEND,
        threshold=2,
        admin_client=admin,
    )

    assert rec.litellm_credits == 20, "a keyless workspace still reconciles to zero"
    assert rec.litellm_rows == 1
    assert rec.bc3_credits == 20
    assert rec.delta == 0
    assert rec.coverage_gap is False

    # The compare is a READ. It must not mint the bookkeeping row the ingest does:
    # that row's ``createdAt`` becomes the live read window's start, so creating it
    # here would move where billing later begins.
    assert await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == WS) is None


async def test_reconcile_bounds_the_customer_read_by_the_callers_window(mongo_db):
    """Shadow compares ``[since, until)``. The ingest's high-water mark bounds the
    ingest; it must neither bound nor be advanced by the compare."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()
    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == WS)
    assert doc is not None
    doc.last_spend_ingest_ts = "2026-09-02T23:00:00"  # well after the row below
    await doc.save()

    admin = FakeAdmin(customer_rows=[_row("req-chat", usd=0.08)])

    rec = await provisioning.reconcile_tenant_spend(
        WS, since=SINCE, until=UNTIL, spend_card=SPEND, threshold=2, admin_client=admin
    )

    assert admin.windows == [("2026-09-01 00:00:00", "2026-09-03 00:00:00")]
    assert rec.litellm_credits == 20  # the mark did not filter the compare

    after = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == WS)
    assert after is not None
    assert after.last_spend_ingest_ts == "2026-09-02T23:00:00"  # never advanced


async def test_reconcile_survives_one_read_failing(mongo_db):
    """One half of the proxy being unreachable must not zero the other half — the
    compare degrades to a partial reading, it does not report no spend."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(
        key_rows=[_row("req-key", usd=0.04)],
        customer_rows=[_row("req-chat", usd=0.08)],
        fail_customer_read=True,
    )
    rec = await provisioning.reconcile_tenant_spend(
        WS, since=SINCE, until=UNTIL, spend_card=SPEND, threshold=2, admin_client=admin
    )
    assert rec.litellm_credits == 10  # the per-key half still read

    admin = FakeAdmin(
        key_rows=[_row("req-key", usd=0.04)],
        customer_rows=[_row("req-chat", usd=0.08)],
        fail_key_read=True,
    )
    rec = await provisioning.reconcile_tenant_spend(
        WS, since=SINCE, until=UNTIL, spend_card=SPEND, threshold=2, admin_client=admin
    )
    assert rec.litellm_credits == 20  # the customer half still read


async def test_reconcile_without_a_window_compares_the_same_span_on_both_sides(mongo_db):
    """The windowless call is where the two meters drifted apart.

    The LiteLLM side used to derive its own start (the tenant's mark, or its row's
    ``createdAt``) while the BC-3 side got the caller's ``None`` and summed all
    time. On a freshly provisioned tenant — no mark, ``createdAt`` ≈ now — that
    derived window is ZERO WIDTH: the proxy was asked for spend over
    ``[now, now]``, returned nothing, and the compare reported a coverage gap
    against an all-time ledger sum. Both halves now resolve from one window.
    """
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await credits.debit(
        WS, 30, cause="compute_spend", idempotency_key="run:r1", allow_negative=True
    )
    await _provision()  # fresh row: no high-water mark, createdAt ≈ now

    admin = FakeAdmin(
        key_rows=[_row("req-key", usd=0.04)],  # 10 credits, dated months back
        customer_rows=[_row("req-chat", usd=0.08)],  # 20 credits, dated months back
    )

    rec = await provisioning.reconcile_tenant_spend(
        WS, spend_card=SPEND, threshold=2, admin_client=admin
    )

    # The customer read covered a real span, not the zero-width one the tenant's
    # own row would have produced.
    assert len(admin.windows) == 1
    start_date, end_date = admin.windows[0]
    assert start_date == "1970-01-01 00:00:00"
    assert start_date < end_date

    # Both meters saw their spend, so the compare agrees instead of manufacturing
    # a gap out of a window only one side used.
    assert rec.litellm_rows == 2
    assert rec.litellm_credits == 30
    assert rec.bc3_credits == 30
    assert rec.delta == 0
    assert rec.coverage_gap is False

    # And the audit row reports the span that was actually compared — an operator
    # reading it back can tell which window produced the delta.
    records = await SpendReconciliation.find(SpendReconciliation.workspace == WS).to_list()
    assert len(records) == 1
    assert records[0].window_start == start_date.replace(" ", "T") + "+00:00"
    assert records[0].window_start == rec.window_start
    assert records[0].window_end == rec.window_end


async def test_reconcile_ends_both_sides_at_the_same_instant(mongo_db):
    """The open UPPER bound, which is where the two sides can still drift apart.

    With no ``until`` the compare ends at the instant it runs. If the ledger half
    keeps the caller's raw ``None`` while the proxy half gets that resolved
    instant, a row dated in the FUTURE is dropped from one meter and counted by
    the other — a delta out of a span only one side measured. Same bug as the
    original, one bound over. Both halves must stop at the same instant.
    """
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    # One settled debit, and one stamped ahead of the compare. Proxy rows are
    # recorded with a ``startTime`` the tenant's own clock supplies, so a row
    # ahead of ours is not hypothetical.
    await credits.debit(
        WS, 10, cause="compute_spend", idempotency_key="run:past", allow_negative=True
    )
    await credits.debit(
        WS, 99, cause="compute_spend", idempotency_key="run:ahead", allow_negative=True
    )
    ahead = await CreditLedgerEntry.find_one(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.idempotency_key == "run:ahead",
    )
    assert ahead is not None
    await ahead.set({CreditLedgerEntry.createdAt: datetime(2099, 1, 1, tzinfo=UTC)})

    admin = FakeAdmin(
        customer_rows=[
            _row("req-past", usd=0.04),  # 10 credits, months back
            _row("req-ahead", usd=0.40, at="2099-01-01T00:00:00"),  # 100, ahead of now
        ]
    )

    rec = await provisioning.reconcile_tenant_spend(
        WS, spend_card=SPEND, threshold=2, admin_client=admin
    )

    # Each side counted the settled row and neither counted the one past the end
    # of the window.
    assert rec.litellm_rows == 1
    assert rec.litellm_credits == 10
    assert rec.bc3_entries == 1
    assert rec.bc3_credits == 10
    assert rec.delta == 0
    assert rec.coverage_gap is False


async def test_reconcile_refuses_an_inverted_window(mongo_db):
    """``since`` after ``until`` compares nothing on either side, so it would
    persist a confident ``delta=0, coverage_gap=False`` — a clean bill of health
    for a question nobody asked — and reach the proxy as ``start_date >
    end_date``, which /spend/logs/v2 answers however it likes."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(customer_rows=[_row("req-chat", usd=0.08)])

    with pytest.raises(ValidationError):
        await provisioning.reconcile_tenant_spend(
            WS, since=UNTIL, until=SINCE, spend_card=SPEND, threshold=2, admin_client=admin
        )

    assert admin.windows == []  # refused before the proxy was touched
    assert await SpendReconciliation.find(SpendReconciliation.workspace == WS).to_list() == []


async def test_reconcile_with_only_until_reads_from_the_epoch(mongo_db):
    """The backfill shape: "everything before last week". The open lower bound
    must widen the read, never invert it — the proxy gets epoch..until, and the
    BC-3 sum is bounded by the same pair."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(customer_rows=[_row("req-chat", usd=0.08, at="2026-08-01T10:00:00")])

    rec = await provisioning.reconcile_tenant_spend(
        WS, until=UNTIL, spend_card=SPEND, threshold=2, admin_client=admin
    )

    assert admin.windows == [("1970-01-01 00:00:00", "2026-09-03 00:00:00")]
    assert rec.litellm_credits == 20
    assert rec.window_start == "1970-01-01T00:00:00+00:00"
    assert rec.window_end == UNTIL.isoformat()
    # The ledger side is bounded by the same pair, so a debit made now — after
    # ``until`` — is outside the compare on BOTH sides rather than on one.
    assert rec.bc3_credits == 0


async def test_reconcile_still_debits_nothing_with_both_reads(mongo_db):
    """The invariant the second read must not break: shadow moves no money."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await credits.debit(
        WS, 10, cause="compute_spend", idempotency_key="run:r1", allow_negative=True
    )
    await _provision()

    admin = FakeAdmin(
        key_rows=[_row("req-key", usd=0.04)],
        customer_rows=[_row("req-chat", usd=0.08)],
    )
    before_entries, before_balance = await _ledger_snapshot(WS)

    await provisioning.reconcile_tenant_spend(
        WS, since=SINCE, until=UNTIL, spend_card=SPEND, threshold=2, admin_client=admin
    )

    after_entries, after_balance = await _ledger_snapshot(WS)
    assert after_entries == before_entries
    assert after_balance == before_balance
    assert (
        await CreditLedgerEntry.find(
            CreditLedgerEntry.workspace == WS,
            CreditLedgerEntry.cause == "litellm_spend",
        ).to_list()
        == []
    )
