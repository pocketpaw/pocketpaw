# tests/cloud/llm_provisioning/test_spend_by_customer.py — proves the ingest can
# see a chat run's cost, and says so when it cannot see something else.
#
# THE BUG. Both agent backends authenticate to the proxy with
# ``settings.litellm_api_key``, the DEPLOYMENT's key. The cutover ingest read a
# tenant's spend as ``/spend/logs?api_key=<that tenant's virtual key>``, which no
# chat request has ever carried. With ``live`` gating BC-3's per-run metering off
# so exactly one meter charges, the one meter was filtering out the product's main
# cost centre. Production logged ``ingested spend for 3/3 tenants -> 0 credits``
# against runs the proxy had priced in dollars, and nothing errored — the read was
# correct, it was just scoped to the wrong thing.
#
# These tests are written against that shape rather than against the new code:
#
#   * a spend row that carries ONLY an ``end_user`` — a chat row — is billed;
#   * a row both reads return is billed ONCE;
#   * a row that lands late, behind an already-advanced high-water mark, is still
#     billed, because the customer read is window-bounded and must not be filtered
#     by the mark a second time;
#   * the per-key read still bills what only it can see, so this is additive;
#   * either read failing leaves the other one working;
#   * and the coverage check counts rows no workspace claims, which is the only
#     signal that separates "nobody spent anything" from "we are not looking where
#     the spending is".
#
# Uses the shared ``mongo_db`` + autouse ``recording_bus`` fixtures from
# tests/cloud/conftest.py. A FAKE admin client stands in for the proxy.
#
# Created 2026-09-02 (feat/proxy-spend-ingest-by-customer): new test module.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.catalog.admin_client import LiteLLMAdminError
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.llm_provisioning import cutover_sweeper
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning
from pocketpaw_ee.cloud.llm_provisioning.domain import KeyBudget, SpendCredits
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey

WS = "ws_customer_spend"

# Pinned so credits never depend on ambient settings: round(usd * 250).
SPEND = SpendCredits(markup=2.5, credit_usd=0.01)


class FakeAdmin:
    """In-memory stand-in for LiteLLMAdminClient, with BOTH spend reads.

    ``key_rows`` is what ``/spend/logs?api_key=`` returns — the rows a tenant's own
    virtual key accrued. ``customer_rows`` is what ``/spend/logs/v2?end_user=``
    returns — every row tagged with the workspace, whichever key paid for it. A
    chat row appears only in the second.

    ``counts`` drives the coverage check: ``None`` is the unfiltered total, a
    workspace id its own count.

    ``customers`` is what ``/customer/list`` returns — every id the proxy has seen
    spend for, whether or not we ever minted that workspace a key. It is the only
    source that knows about a workspace which pays for chat and nothing else.
    """

    def __init__(
        self,
        *,
        key_rows: list[dict] | None = None,
        customer_rows: list[dict] | None = None,
        counts: dict[str | None, int] | None = None,
        customers: list[str] | None = None,
        fail_customer_read: bool = False,
        fail_key_read: bool = False,
        fail_customer_list: bool = False,
        fail_counts_for: set[str | None] | None = None,
    ) -> None:
        self.key_rows = key_rows or []
        self.customer_rows = customer_rows or []
        self.counts = counts or {}
        self.customers = customers or []
        self.fail_customer_read = fail_customer_read
        self.fail_key_read = fail_key_read
        self.fail_customer_list = fail_customer_list
        self.fail_counts_for = fail_counts_for or set()
        self.windows: list[tuple[str, str]] = []
        self.count_calls: list[str | None] = []

    async def generate_key(self, **kwargs):
        return {"key": f"sk-{kwargs.get('key_alias', 'x')}", **kwargs}

    async def spend_logs(self, *, api_key: str):
        if self.fail_key_read:
            raise LiteLLMAdminError("per-key read failed (simulated)")
        return list(self.key_rows)

    async def spend_logs_by_end_user(self, *, end_user, start_date, end_date, page_size=100):
        self.windows.append((start_date, end_date))
        if self.fail_customer_read:
            raise LiteLLMAdminError("customer read failed (simulated)")
        return list(self.customer_rows)

    async def spend_log_count(self, *, start_date, end_date, end_user=None):
        self.count_calls.append(end_user)
        if end_user in self.fail_counts_for:
            raise LiteLLMAdminError("count failed (simulated)")
        return self.counts.get(end_user, 0)

    async def list_customers(self):
        if self.fail_customer_list:
            raise LiteLLMAdminError("customer list failed (simulated)")
        return list(self.customers)


async def _provision(workspace: str = WS) -> LiteLLMTenantKey:
    await provisioning.ensure_tenant_key(workspace, budget=KeyBudget(), admin_client=FakeAdmin())
    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == workspace)
    assert doc is not None
    return doc


def _row(rid: str, *, usd: float = 0.04, at: str = "2026-09-02T10:00:00") -> dict:
    return {"request_id": rid, "spend": usd, "startTime": at, "model": "gpt-5.2"}


async def _debits_for(workspace: str, rid: str) -> list[CreditLedgerEntry]:
    return await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == workspace,
        CreditLedgerEntry.idempotency_key == f"litellm:{rid}",
    ).to_list()


# ===========================================================================
# The bug: a chat run's cost.
# ===========================================================================


async def test_a_chat_row_is_billed_even_though_no_tenant_key_paid_for_it(mongo_db):
    """The whole point. A chat request rides the deployment's master key, so it
    appears in NO tenant's per-key spend log — only under the workspace it named
    in its ``user`` field. Before the customer read, this row billed nothing."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(key_rows=[], customer_rows=[_row("req-chat")])

    result = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    assert result.rows_billed == 1
    assert result.credits_debited == 10  # round(0.04 * 250)
    assert await credits.balance(WS) == 990


async def test_a_row_both_reads_return_is_billed_exactly_once(mongo_db):
    """Studio and the media server send the tenant key AND tag ``user``, so their
    rows come back from both reads. Merging on ``request_id`` keeps the row count
    honest; the ledger key is what makes the money exactly-once either way."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    shared = _row("req-both")
    admin = FakeAdmin(key_rows=[shared], customer_rows=[dict(shared)])

    result = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    assert result.rows_read == 1, "the same row was counted twice"
    assert result.credits_debited == 10
    assert await credits.balance(WS) == 990
    assert len(await _debits_for(WS, "req-both")) == 1


async def test_the_key_read_still_bills_what_only_it_can_see(mongo_db):
    """Additive, not a replacement. A row on the tenant key with no ``user`` tag
    would vanish if the customer read had replaced the key read, and vanishing is
    what this branch exists to stop."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(key_rows=[_row("req-untagged")], customer_rows=[])

    result = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    assert result.credits_debited == 10
    assert len(await _debits_for(WS, "req-untagged")) == 1


async def test_a_late_row_behind_the_mark_is_still_billed(mongo_db):
    """Spend rows are stamped with when a call STARTED and written when it ENDED,
    in batches — so a row can surface after the high-water mark has moved past its
    own timestamp. The key read has to honour the mark (it is unbounded and would
    otherwise re-walk history), but the customer read is already window-bounded,
    and filtering it by the mark as well would drop that row on this sweep and on
    every sweep after it. Silently, and forever.
    """
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    doc = await _provision()
    doc.last_spend_ingest_ts = "2026-09-02T12:00:00+00:00"
    await doc.save()

    late = _row("req-late", at="2026-09-02T11:30:00")  # behind the mark
    admin = FakeAdmin(key_rows=[late], customer_rows=[dict(late)])

    result = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    assert result.rows_billed == 1, "a late row was dropped by the high-water mark"
    assert len(await _debits_for(WS, "req-late")) == 1


async def test_a_row_already_ingested_is_not_billed_again_by_the_overlap(mongo_db):
    """The flip side of the test above. The customer read deliberately re-reads a
    few minutes before the mark, so settled rows come back every sweep — and must
    move no money when they do."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(customer_rows=[_row("req-settled")])

    first = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)
    second = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    assert first.credits_debited == 10
    assert second.credits_debited == 0
    assert await credits.balance(WS) == 990
    assert len(await _debits_for(WS, "req-settled")) == 1


# ===========================================================================
# The window the customer read asks for.
# ===========================================================================


async def test_an_unmarked_tenant_reads_from_when_we_provisioned_them(mongo_db):
    """Not from the beginning of time. A tenant with no mark is one we have never
    ingested for, and the earliest spend that can be theirs is the moment their key
    doc was created — reaching further back could only pick up spend some other
    meter already charged."""
    doc = await _provision()
    doc.createdAt = datetime(2026, 8, 20, 9, 0, 0, tzinfo=UTC)
    doc.last_spend_ingest_ts = None
    await doc.save()

    admin = FakeAdmin(customer_rows=[])
    await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    start, _end = admin.windows[0]
    assert start == "2026-08-20 09:00:00"


async def test_a_marked_tenant_reads_from_just_before_its_mark(mongo_db):
    await _provision()
    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == WS)
    doc.last_spend_ingest_ts = "2026-09-02T12:00:00+00:00"
    await doc.save()

    admin = FakeAdmin(customer_rows=[])
    await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    start, _end = admin.windows[0]
    expected = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC) - provisioning._SPEND_READ_OVERLAP
    assert start == expected.strftime("%Y-%m-%d %H:%M:%S")


# ===========================================================================
# One read failing must not take the other down.
# ===========================================================================


async def test_a_failing_customer_read_leaves_the_key_read_billing(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(key_rows=[_row("req-key")], fail_customer_read=True)

    result = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    assert result.credits_debited == 10
    assert len(await _debits_for(WS, "req-key")) == 1


async def test_a_failing_key_read_leaves_the_customer_read_billing(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(customer_rows=[_row("req-customer")], fail_key_read=True)

    result = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    assert result.credits_debited == 10
    assert len(await _debits_for(WS, "req-customer")) == 1


# ===========================================================================
# Coverage — the number that would have said something was wrong.
# ===========================================================================


async def test_coverage_counts_the_rows_no_workspace_claims(mongo_db):
    admin = FakeAdmin(counts={None: 10, "ws_a": 4, "ws_b": 3})

    coverage = await provisioning.spend_attribution_coverage(
        ["ws_a", "ws_b"],
        since=datetime(2026, 9, 2, tzinfo=UTC),
        until=datetime(2026, 9, 3, tzinfo=UTC),
        admin_client=admin,
    )

    assert coverage.total_rows == 10
    assert coverage.attributed_rows == 7
    assert coverage.unattributed_rows == 3
    assert coverage.workspaces_checked == 2
    assert coverage.degraded is False
    # The unfiltered total plus one count per workspace, and nothing more — this
    # runs on every sweep tick.
    assert admin.count_calls == [None, "ws_a", "ws_b"]


async def test_full_attribution_reports_no_gap(mongo_db):
    admin = FakeAdmin(counts={None: 7, "ws_a": 4, "ws_b": 3})

    coverage = await provisioning.spend_attribution_coverage(
        ["ws_a", "ws_b"],
        since=datetime(2026, 9, 2, tzinfo=UTC),
        until=datetime(2026, 9, 3, tzinfo=UTC),
        admin_client=admin,
    )

    assert coverage.unattributed_rows == 0
    assert coverage.degraded is False


async def test_a_failed_count_is_reported_as_unknown_not_as_clean(mongo_db):
    """ "No gap" and "could not tell" look identical in a bare zero. The check that
    exists to catch a silent failure must not fail silently itself."""
    admin = FakeAdmin(counts={None: 10, "ws_a": 4}, fail_counts_for={"ws_b"})

    coverage = await provisioning.spend_attribution_coverage(
        ["ws_a", "ws_b"],
        since=datetime(2026, 9, 2, tzinfo=UTC),
        until=datetime(2026, 9, 3, tzinfo=UTC),
        admin_client=admin,
    )

    assert coverage.degraded is True
    assert coverage.attributed_rows == 4  # ws_b's rows are simply unknown


async def test_coverage_never_reports_a_negative_remainder(mongo_db):
    # The total and the per-tenant counts are separate queries against a table
    # still being written. A row landing between them is a race, not a surplus.
    admin = FakeAdmin(counts={None: 2, "ws_a": 5})

    coverage = await provisioning.spend_attribution_coverage(
        ["ws_a"],
        since=datetime(2026, 9, 2, tzinfo=UTC),
        until=datetime(2026, 9, 3, tzinfo=UTC),
        admin_client=admin,
    )

    assert coverage.unattributed_rows == 0


async def test_coverage_never_raises_into_the_sweep(mongo_db):
    """It is an observation OF the sweep, not part of it. Billing must not stop
    because the thing watching billing could not reach the proxy."""
    admin = FakeAdmin(fail_counts_for={None, "ws_a"})

    coverage = await provisioning.spend_attribution_coverage(
        ["ws_a"],
        since=datetime(2026, 9, 2, tzinfo=UTC),
        until=datetime(2026, 9, 3, tzinfo=UTC),
        admin_client=admin,
    )

    assert coverage.degraded is True
    assert coverage.total_rows == 0


# ===========================================================================
# The sweep surfaces it.
# ===========================================================================


@pytest.mark.parametrize("mode", ["shadow", "live"])
async def test_the_sweep_reports_unattributed_rows_in_every_billing_mode(
    mongo_db, monkeypatch, mode
):
    """Live is where it costs money; shadow is where it is a go/no-go. Flipping to
    live while rows are unattributed converts a reporting gap into free service, so
    the number has to be on screen BEFORE the flip, not after."""
    await _provision()

    from pocketpaw_ee.cloud.llm_provisioning.domain import SpendCoverage

    async def _coverage(workspaces, *, since, until, admin_client=None):
        return SpendCoverage(
            window_start="w0",
            window_end="w1",
            total_rows=9,
            attributed_rows=4,
            unattributed_rows=5,
            workspaces_checked=len(workspaces),
            degraded=False,
        )

    monkeypatch.setattr(provisioning, "spend_attribution_coverage", _coverage)
    monkeypatch.setattr(
        cutover_sweeper.provisioning_service, "spend_attribution_coverage", _coverage
    )

    summary = await cutover_sweeper.run_cutover_sweep(mode=mode)

    assert summary["unattributed"] == 5


async def test_the_sweep_checks_coverage_even_with_no_provisioned_tenants(mongo_db, monkeypatch):
    """A deployment serving traffic with nothing provisioned is the loudest form of
    this failure, and it is exactly the case the tenant loop returns early on."""
    seen: dict = {}

    from pocketpaw_ee.cloud.llm_provisioning.domain import SpendCoverage

    async def _coverage(workspaces, *, since, until, admin_client=None):
        seen["called"] = True
        return SpendCoverage(
            window_start="w0",
            window_end="w1",
            total_rows=12,
            attributed_rows=0,
            unattributed_rows=12,
            workspaces_checked=0,
            degraded=False,
        )

    monkeypatch.setattr(
        cutover_sweeper.provisioning_service, "spend_attribution_coverage", _coverage
    )

    summary = await cutover_sweeper.run_cutover_sweep(mode="live")

    assert seen.get("called") is True
    assert summary["tenants"] == 0
    assert summary["unattributed"] == 12


async def test_off_mode_still_sweeps_nothing_and_checks_nothing(mongo_db, monkeypatch):
    # ``off`` means BC-3 bills as it always has. The coverage check talks to the
    # proxy, so it must not start doing that on a deployment that has not opted in.
    called: dict = {}

    async def _coverage(workspaces, *, since, until, admin_client=None):
        called["yes"] = True
        raise AssertionError("coverage must not run in off mode")

    monkeypatch.setattr(
        cutover_sweeper.provisioning_service, "spend_attribution_coverage", _coverage
    )

    summary = await cutover_sweeper.run_cutover_sweep(mode="off")

    assert called == {}
    assert summary["unattributed"] == 0
