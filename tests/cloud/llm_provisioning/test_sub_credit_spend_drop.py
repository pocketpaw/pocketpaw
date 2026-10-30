# tests/cloud/llm_provisioning/test_sub_credit_spend_drop.py — proves that proxy
# spend below one credit is served for free, permanently.
#
# THE BUG. ``ingest_tenant_spend`` converts each spend row to credits on its own
# and drops the row when the result rounds to zero:
#
#     credits = card.to_credits(cost_usd)
#     if credits <= 0:
#         continue  # sub-credit / zero-cost row — nothing to debit
#
# With the default rate card (markup 2.5, credit_usd 0.01) that is
# ``round(cost_usd * 250)``, so every row under $0.002 bills nothing. The row is
# not deferred and nothing accumulates: the high-water mark advances past it in
# the same pass, and the ledger never sees it. Ten thousand such rows are ten
# thousand separate zeros.
#
# This matters more after the billing cutover than before it, and that is the
# regression. BC-3 priced a whole RUN, so the rounding applied once to the sum of
# everything a run spent. LiteLLM prices one API CALL, so the same rounding now
# applies to each call separately — a much smaller unit, far more often under the
# threshold. The conversion did not change; the grain it is applied at did.
#
# The failure is silent by construction. ``cost_usd`` on the result still carries
# the real dollars that were read, so the sweep logs a confident
# ``ingested spend for 5/5 tenants -> 0 credits`` over money the proxy has
# already priced and served.
#
# Uses the shared ``mongo_db`` fixture from tests/cloud/conftest.py and a FAKE
# admin client, mirroring test_spend_by_customer.py.
#
# Created 2026-09-04 (fix/sub-credit-spend-drop): new test module.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.credits.domain import credits_to_micro
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning
from pocketpaw_ee.cloud.llm_provisioning.domain import KeyBudget, SpendCredits
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey

WS = "ws_sub_credit"

# Pinned so credits never depend on ambient settings: round(usd * 250).
SPEND = SpendCredits(markup=2.5, credit_usd=0.01)

# One cheap call. $0.0015 * 250 = 0.375 credits, which rounds to zero.
CHEAP_USD = 0.0015
CHEAP_ROWS = 8
# What the same money is worth billed as a sum instead of row by row.
EXPECTED_CREDITS = round(CHEAP_USD * CHEAP_ROWS * 250)  # 3
# And what each row is worth on its own, exactly, in the wallet's storage unit.
EXPECTED_MICRO_PER_ROW = round(CHEAP_USD * 250 * 1_000_000)  # 375_000
EXPECTED_MICRO_TOTAL = EXPECTED_MICRO_PER_ROW * CHEAP_ROWS


class FakeAdmin:
    """In-memory stand-in for LiteLLMAdminClient.

    Carries BOTH spend reads so this module runs unchanged against the pre- and
    post-customer-read shapes of ``ingest_tenant_spend``.
    """

    def __init__(self, *, key_rows=None, customer_rows=None):
        self.key_rows = key_rows or []
        self.customer_rows = customer_rows or []

    async def generate_key(self, **kwargs):
        return {"key": f"sk-{kwargs.get('key_alias', 'x')}", **kwargs}

    async def spend_logs(self, *, api_key: str):
        return list(self.key_rows)

    async def spend_logs_by_end_user(self, *, end_user, start_date, end_date, page_size=100):
        return list(self.customer_rows)

    async def spend_log_count(self, *, start_date, end_date, end_user=None):
        return 0

    async def list_customers(self):
        return []


async def _provision(workspace: str = WS) -> LiteLLMTenantKey:
    await provisioning.ensure_tenant_key(workspace, budget=KeyBudget(), admin_client=FakeAdmin())
    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == workspace)
    assert doc is not None
    return doc


def _cheap_rows() -> list[dict]:
    """Eight real calls, each priced under half a cent."""
    return [
        {
            "request_id": f"req-cheap-{i}",
            "spend": CHEAP_USD,
            "startTime": f"2026-09-02T10:0{i}:00",
            "model": "gpt-5.2-mini",
        }
        for i in range(CHEAP_ROWS)
    ]


async def test_sub_credit_rows_are_not_served_for_free(mongo_db):
    """Twelve tenths of a cent of real, already-served compute must reach the
    ledger. Today each row rounds to zero on its own and the whole sum is
    discarded, so the wallet is untouched and the sweep reports zero credits over
    money the proxy has already priced."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(key_rows=_cheap_rows())

    result = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    # The read itself is fine — the money is seen.
    assert result.rows_read == CHEAP_ROWS
    assert result.cost_usd == pytest.approx(CHEAP_USD * CHEAP_ROWS)

    # And then it is thrown away.
    assert result.credits_debited == EXPECTED_CREDITS, (
        f"read ${result.cost_usd:.4f} of served compute and debited "
        f"{result.credits_debited} credits"
    )
    assert await credits.balance(WS) == 1000 - EXPECTED_CREDITS


async def test_a_dropped_sub_credit_row_never_comes_back(mongo_db):
    """The drop is permanent, not deferred. The high-water mark advances past a
    dropped row in the same pass, so a later sweep cannot re-examine it — and even
    inside the customer read's window it rounds to zero again. Nothing accumulates
    a remainder, so this money has no path to the ledger at all."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(key_rows=_cheap_rows())

    await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)
    second = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == WS)
    assert doc is not None
    assert doc.last_spend_ingest_ts is not None, "the mark advanced past the dropped rows"

    total_charged = 1000 - await credits.balance(WS)
    assert total_charged == EXPECTED_CREDITS, (
        f"${CHEAP_USD * CHEAP_ROWS:.4f} of served compute billed {total_charged} "
        f"credits across two sweeps (second sweep read {second.rows_read} row(s))"
    )


async def test_each_row_bills_its_own_exact_cost(mongo_db):
    """The point of the finer unit: no row waits on another to become billable.

    Eight $0.0015 calls used to bill nothing at all, then briefly billed 3 credits
    only once the eighth arrived to push the carried total over the line. Now each
    one is 375_000 micro-credits the moment it is read."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    for i, row in enumerate(_cheap_rows(), start=1):
        await provisioning.ingest_tenant_spend(
            WS, spend_card=SPEND, admin_client=FakeAdmin(key_rows=[row])
        )
        assert (
            await credits.balance_micro(WS) == credits_to_micro(1000) - i * EXPECTED_MICRO_PER_ROW
        )


async def test_the_wallet_never_owes_a_fraction(mongo_db):
    """Balance plus billings equals the grant, exactly, with nothing held back.

    The carry this file was written against could only be correct in aggregate —
    it always had money parked outside the ledger between sweeps. There is no
    outside now: the wallet holds the exact figure after every single row.
    """
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    await provisioning.ingest_tenant_spend(
        WS, spend_card=SPEND, admin_client=FakeAdmin(key_rows=_cheap_rows())
    )

    spent_micro = credits_to_micro(1000) - await credits.balance_micro(WS)
    assert spent_micro == EXPECTED_MICRO_TOTAL
    # And the exact dollars, back out through the rate card, are what the proxy
    # charged us — no rounding in either direction.
    assert spent_micro / 1_000_000 * SPEND.credit_usd / SPEND.markup == pytest.approx(
        CHEAP_USD * CHEAP_ROWS
    )
