# tests/cloud/llm_provisioning/test_unswept_workspace_spend.py — proves the sweep
# bills a workspace that spends without ever having been provisioned a key.
#
# THE BUG. Chat authenticates to the proxy with the DEPLOYMENT's key and names the
# paying workspace in the request body's ``user`` field, so a workspace never needs
# a virtual key of its own to run up a bill. The cutover sweep, though, built its
# tenant list from ``list_provisioned_workspaces`` — the workspaces that HAVE a
# key. On the deployment where this was caught the two sets did not intersect at
# all: three provisioned tenants with no proxy spend, and three spending customers
# with no provisioned key. Every tick logged a confident
# ``ingested spend for 3/3 tenants -> 0 credits`` and served every chat dollar
# free, while the attribution-coverage check reported the tagged rows as though
# nobody had sent a ``user`` field at all.
#
# Two independent failures, so these are written against both shapes:
#
#   * the sweep must VISIT a workspace the proxy has spend for, and
#     ``ingest_tenant_spend`` must bill one that holds no key (it used to return a
#     zero result and stop);
#   * the coverage check must SEPARATE rows that name an unswept workspace from
#     rows that name none, because a tagged-but-unswept row is our bug and an
#     untagged row is the caller's, and the old single number sent the reader
#     after the wrong one.
#
# Plus the guards that keep the discovery path from doing harm: an id the proxy
# reports that is not a real workspace must never reach the credit ledger, and a
# proxy that cannot answer must degrade to the OLD behaviour (under-bill) rather
# than to a wrong one.
#
# Uses the shared ``mongo_db`` + autouse ``recording_bus`` fixtures from
# tests/cloud/conftest.py, and the ``FakeAdmin`` proxy stand-in from
# test_spend_by_customer.py.
#
# Created 2026-09-02 (fix/bill-workspaces-the-sweep-cannot-see): new test module.

from __future__ import annotations

from pocketpaw_ee.cloud.llm_provisioning import cutover_sweeper
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning
from pocketpaw_ee.cloud.llm_provisioning.domain import KeyBudget, SpendCredits
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey
from pocketpaw_ee.cloud.models.workspace import Workspace

from tests.cloud.llm_provisioning.test_spend_by_customer import FakeAdmin, _row

# Pinned so credits never depend on ambient settings: round(usd * 250).
SPEND = SpendCredits(markup=2.5, credit_usd=0.01)


async def _workspace(slug: str) -> str:
    """A real workspace row, returning its id as the proxy would report it.

    The id has to be a genuine Mongo id rather than a readable label, because the
    discovery path deliberately refuses to bill an id that names no workspace —
    which is exactly what a hand-written ``"ws_alpha"`` is.
    """
    doc = Workspace(name=slug, slug=slug, owner="u_owner")
    await doc.insert()
    return str(doc.id)


async def _credits(workspace: str) -> int:
    """Credits actually debited to this wallet. ``amount_delta`` is signed."""
    entries = await CreditLedgerEntry.find(CreditLedgerEntry.workspace == workspace).to_list()
    return -sum(e.amount_delta for e in entries if e.amount_delta < 0)


# ===========================================================================
# The bug: a workspace that spends without a key.
# ===========================================================================


async def test_an_unprovisioned_workspace_with_proxy_spend_is_billed(mongo_db):
    """The whole bug in one test: spend, no key, and the sweep must still bill it."""
    ws = await _workspace("pays-for-chat-only")
    admin = FakeAdmin(customer_rows=[_row("chat-1", usd=0.04)], customers=[ws])

    result = await provisioning.ingest_tenant_spend(ws, spend_card=SPEND, admin_client=admin)

    assert result.rows_billed == 1
    assert result.credits_debited == 10
    assert await _credits(ws) == 10


async def test_the_sweep_visits_a_workspace_it_never_provisioned(mongo_db):
    """``list_sweepable_workspaces`` is the union, not the provisioning table."""
    spender = await _workspace("spender-no-key")
    await provisioning.ensure_tenant_key(
        "ws_with_a_key", budget=KeyBudget(), admin_client=FakeAdmin()
    )
    admin = FakeAdmin(customers=[spender])

    sweepable = await provisioning.list_sweepable_workspaces(admin_client=admin)

    assert "ws_with_a_key" in sweepable, "a provisioned tenant must still be swept"
    assert spender in sweepable, "a spending workspace with no key must now be swept too"


async def test_a_keyless_workspace_gets_a_row_to_hold_its_high_water_mark(mongo_db):
    """The discovered tenant needs somewhere to record what it already paid for."""
    ws = await _workspace("needs-bookkeeping")
    admin = FakeAdmin(customer_rows=[_row("chat-1")], customers=[ws])

    await provisioning.ingest_tenant_spend(ws, spend_card=SPEND, admin_client=admin)

    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == ws)
    assert doc is not None, "no row means the next sweep re-reads this tenant's history"
    assert doc.litellm_key is None, "discovery must not fake a key it never minted"
    assert doc.last_spend_ingest_ts is not None, "the mark is the point of the row"


async def test_the_keyless_row_does_not_pretend_to_be_provisioned(mongo_db):
    """A discovered tenant must not show up as one we minted a key for."""
    ws = await _workspace("discovered-not-provisioned")
    admin = FakeAdmin(customer_rows=[_row("chat-1")], customers=[ws])

    await provisioning.ingest_tenant_spend(ws, spend_card=SPEND, admin_client=admin)

    assert await provisioning.list_provisioned_workspaces() == []


async def test_minting_a_key_later_reuses_the_discovered_row(mongo_db):
    """``workspace`` is UNIQUE, so discovery must not block a later real mint."""
    ws = await _workspace("discovered-then-minted")
    admin = FakeAdmin(customer_rows=[_row("chat-1")], customers=[ws])
    await provisioning.ingest_tenant_spend(ws, spend_card=SPEND, admin_client=admin)

    await provisioning.ensure_tenant_key(ws, budget=KeyBudget(), admin_client=FakeAdmin())

    docs = await LiteLLMTenantKey.find(LiteLLMTenantKey.workspace == ws).to_list()
    assert len(docs) == 1, "a second row would split this tenant's high-water mark in two"
    assert docs[0].litellm_key, "the mint has to land on the row discovery created"


async def test_a_second_sweep_does_not_bill_the_same_row_twice(mongo_db):
    """Discovery must not reset exactly-once billing for the tenant it found."""
    ws = await _workspace("swept-twice")
    admin = FakeAdmin(customer_rows=[_row("chat-1", usd=0.04)], customers=[ws])

    await provisioning.ingest_tenant_spend(ws, spend_card=SPEND, admin_client=admin)
    again = await provisioning.ingest_tenant_spend(ws, spend_card=SPEND, admin_client=admin)

    assert again.credits_debited == 0
    assert await _credits(ws) == 10


# ===========================================================================
# Guards: discovery must not bill things that are not tenants.
# ===========================================================================


async def test_a_customer_id_that_is_no_workspace_is_never_swept(mongo_db):
    """The ``user`` field crossed the wire, so an id off it is checked, not trusted."""
    real = await _workspace("a-real-one")
    ghost = "6a1210f462bf55588dee1d4f"  # correctly shaped, and no such workspace
    admin = FakeAdmin(customers=[real, ghost])

    sweepable = await provisioning.list_sweepable_workspaces(admin_client=admin)

    assert real in sweepable
    assert ghost not in sweepable, "a debit here invents a wallet for a tenant that never existed"


async def test_a_malformed_customer_id_is_skipped_not_raised(mongo_db):
    """A junk id must not take the whole sweep down with it."""
    real = await _workspace("survives-the-junk")
    admin = FakeAdmin(customers=["not-an-object-id", "", real])

    sweepable = await provisioning.list_sweepable_workspaces(admin_client=admin)

    assert sweepable == [real]


async def test_a_dead_customer_list_falls_back_to_the_provisioned_tenants(mongo_db):
    """A proxy that cannot answer must degrade to under-billing, not to guessing."""
    await provisioning.ensure_tenant_key(
        "ws_provisioned", budget=KeyBudget(), admin_client=FakeAdmin()
    )
    admin = FakeAdmin(fail_customer_list=True)

    sweepable = await provisioning.list_sweepable_workspaces(admin_client=admin)

    assert sweepable == ["ws_provisioned"]


# ===========================================================================
# The second failure: the coverage check blamed the wrong thing.
# ===========================================================================


async def test_coverage_separates_an_unswept_workspace_from_an_untagged_row(mongo_db):
    """10 rows: 2 attributed, 5 tagged to a workspace nobody sweeps, 3 tagged to nobody."""
    unswept = "6a146169ad1f4c4decb828f4"
    admin = FakeAdmin(
        counts={None: 10, "ws_swept": 2, unswept: 5},
        customers=["ws_swept", unswept],
    )

    coverage = await provisioning.spend_attribution_coverage(
        ["ws_swept"],
        since=_since(),
        until=_until(),
        admin_client=admin,
    )

    assert coverage.unattributed_rows == 8
    assert coverage.unswept_rows == 5, "these rows named who pays and we did not look"
    assert coverage.unswept_workspaces == (unswept,)
    assert coverage.unattributed_rows - coverage.unswept_rows == 3
    assert coverage.degraded is False


async def test_coverage_reports_no_unswept_rows_when_every_customer_is_swept(mongo_db):
    admin = FakeAdmin(counts={None: 10, "ws_swept": 7}, customers=["ws_swept"])

    coverage = await provisioning.spend_attribution_coverage(
        ["ws_swept"], since=_since(), until=_until(), admin_client=admin
    )

    assert coverage.unattributed_rows == 3
    assert coverage.unswept_rows == 0
    assert coverage.unswept_workspaces == ()


async def test_an_unreadable_customer_list_is_degraded_not_a_clean_split(mongo_db):
    """ "Could not split" and "nothing to split" must not look identical."""
    admin = FakeAdmin(counts={None: 10, "ws_swept": 7}, fail_customer_list=True)

    coverage = await provisioning.spend_attribution_coverage(
        ["ws_swept"], since=_since(), until=_until(), admin_client=admin
    )

    assert coverage.unattributed_rows == 3
    assert coverage.unswept_rows == 0
    assert coverage.degraded is True


async def test_the_split_never_exceeds_the_remainder(mongo_db):
    """Separate queries against a table still being written can disagree."""
    admin = FakeAdmin(
        counts={None: 4, "ws_swept": 3, "6a146169ad1f4c4decb828f4": 9},
        customers=["6a146169ad1f4c4decb828f4"],
    )

    coverage = await provisioning.spend_attribution_coverage(
        ["ws_swept"], since=_since(), until=_until(), admin_client=admin
    )

    assert coverage.unattributed_rows == 1
    assert coverage.unswept_rows == 1, "a split bigger than the remainder is a race, not a finding"


# ===========================================================================
# End to end through the sweep.
# ===========================================================================


async def test_the_live_sweep_bills_the_workspace_it_had_to_discover(mongo_db, monkeypatch):
    """The production symptom, inverted: tenants swept, and credits actually move."""
    ws = await _workspace("live-sweep-discovers-me")
    admin = FakeAdmin(
        customer_rows=[_row("chat-1", usd=0.04)],
        counts={None: 1, ws: 1},
        customers=[ws],
    )
    monkeypatch.setattr(provisioning, "LiteLLMAdminClient", lambda *a, **k: admin)

    summary = await cutover_sweeper.run_cutover_sweep(mode="live")

    assert summary["tenants"] == 1, "the discovered workspace is a tenant to sweep"
    assert summary["processed"] == 1
    assert summary["credits"] == 10, "the whole bug was that this stayed at zero"
    assert summary["unattributed"] == 0


def _since():
    from datetime import UTC, datetime

    return datetime(2026, 9, 2, tzinfo=UTC)


def _until():
    from datetime import UTC, datetime

    return datetime(2026, 9, 3, tzinfo=UTC)
