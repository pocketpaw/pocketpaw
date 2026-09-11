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
# Uses the shared ``mongo_db`` + autouse ``recording_bus`` fixtures from
# tests/cloud/conftest.py. A FAKE admin client stands in for the proxy.
#
# Created 2026-09-11 (fix/billing-reconcile-two-reads): new test module.

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw_ee.catalog.admin_client import LiteLLMAdminError
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning
from pocketpaw_ee.cloud.llm_provisioning.domain import KeyBudget, SpendCredits
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey

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
