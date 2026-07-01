# tests/cloud/llm_provisioning/test_provisioning.py — proves the MCG-8 per-tenant
# LiteLLM virtual-key provisioning + spend->credits ingestion seam.
#
# PROVISIONING:
#   1. ensure_tenant_key mints a key via the admin API with the budget / rpm / tpm
#      / metadata={workspace_id} and stores the workspace -> key mapping.
#   2. A second ensure_tenant_key is idempotent — returns the SAME key, makes NO
#      second proxy call (created=False), and never stores a second row.
#   3. get_tenant_key reads the stored key back (None when unprovisioned).
#
# SPEND INGESTION (the real seam — plugs into the EXISTING credits ledger):
#   4. ingest_tenant_spend reads /spend/logs and debits the EXISTING credit ledger
#      (credits.service) with the right workspace + credits + cause + a
#      litellm:{request_id} idempotency key; cached tokens are captured.
#   5. Re-ingest is idempotent — the BC-1 ledger key blocks a second debit even
#      when the high-water mark is reset, so the balance never moves twice.
#   6. The high-water mark advances so a re-sweep skips settled rows.
#   7. An unprovisioned workspace returns a zero result (no raise).
#   8. allow_negative: spend beyond the wallet still bills fully (metered compute).
#
# Uses the shared ``mongo_db`` fixture (mongomock-motor + Beanie over
# ALL_DOCUMENTS) + the autouse ``recording_bus`` from tests/cloud/conftest.py —
# the same DB-fixture pattern the credits / metering tests use. A FAKE admin
# client (no HTTP) stands in for the proxy so provisioning + ingestion are tested
# without a live proxy; the admin client's own HTTP wiring is covered separately
# in tests/cloud/catalog/test_admin_client.py.
#
# Created 2026-06-26 (integration/model-catalog-v2, MCG-8): new test module.

from __future__ import annotations

from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning
from pocketpaw_ee.cloud.llm_provisioning.domain import KeyBudget, SpendCredits
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey

WS = "ws_prov_test"

# Pin the spend rate card so credits don't depend on ambient POCKETPAW_* settings:
# credits = round(cost_usd * 250).
SPEND = SpendCredits(markup=2.5, credit_usd=0.01)

# A budget the FakeAdmin echoes back; values are config-shaped, not secrets.
BUDGET = KeyBudget(
    max_budget_usd=25.0, budget_duration="30d", rpm_limit=60, tpm_limit=10_000, models=[]
)


class FakeAdmin:
    """In-memory stand-in for LiteLLMAdminClient.

    ``generate_key`` mints a deterministic key + records the payload it was called
    with so the test can assert the budget / metadata round-tripped. ``spend_logs``
    returns a scripted list of rows. ``generate_calls`` counts mints so the
    idempotency test can prove the proxy is NOT hit twice.
    """

    def __init__(self, *, spend_rows: list[dict] | None = None) -> None:
        self.generate_calls = 0
        self.last_generate_kwargs: dict | None = None
        self._spend_rows = spend_rows or []
        self.spend_log_calls: list[str] = []

    async def generate_key(self, **kwargs):
        self.generate_calls += 1
        self.last_generate_kwargs = kwargs
        return {"key": f"sk-{kwargs.get('key_alias', 'x')}", **{k: v for k, v in kwargs.items()}}

    async def spend_logs(self, *, api_key: str):
        self.spend_log_calls.append(api_key)
        return list(self._spend_rows)


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


async def test_ensure_tenant_key_mints_with_budget_and_stores_mapping(mongo_db):
    admin = FakeAdmin()
    result = await provisioning.ensure_tenant_key(WS, budget=BUDGET, admin_client=admin)

    assert result.created is True
    assert result.workspace_id == WS
    assert result.litellm_key == "sk-ws-ws_prov_test"
    assert admin.generate_calls == 1

    # The mint carried the budget / limits + metadata={workspace_id}.
    kw = admin.last_generate_kwargs
    assert kw["key_alias"] == "ws-ws_prov_test"
    assert kw["max_budget"] == 25.0
    assert kw["budget_duration"] == "30d"
    assert kw["rpm_limit"] == 60
    assert kw["tpm_limit"] == 10_000
    assert kw["metadata"] == {"workspace_id": WS}

    # The workspace -> key mapping is persisted (exactly one row).
    rows = await LiteLLMTenantKey.find(LiteLLMTenantKey.workspace == WS).to_list()
    assert len(rows) == 1
    assert rows[0].litellm_key == "sk-ws-ws_prov_test"
    assert rows[0].max_budget_usd == 25.0
    assert rows[0].rpm_limit == 60


async def test_ensure_tenant_key_is_idempotent(mongo_db):
    admin = FakeAdmin()
    first = await provisioning.ensure_tenant_key(WS, budget=BUDGET, admin_client=admin)
    second = await provisioning.ensure_tenant_key(WS, budget=BUDGET, admin_client=admin)

    # Same key, no second proxy mint, created=False on the replay.
    assert first.litellm_key == second.litellm_key
    assert second.created is False
    assert admin.generate_calls == 1  # NOT 2 — the proxy is not hit again

    # Still exactly one mapping row.
    rows = await LiteLLMTenantKey.find(LiteLLMTenantKey.workspace == WS).to_list()
    assert len(rows) == 1


async def test_get_tenant_key_reads_back_or_none(mongo_db):
    assert await provisioning.get_tenant_key(WS) is None  # unprovisioned
    await provisioning.ensure_tenant_key(WS, budget=BUDGET, admin_client=FakeAdmin())
    assert await provisioning.get_tenant_key(WS) == "sk-ws-ws_prov_test"


# ---------------------------------------------------------------------------
# Spend ingestion — the seam into the EXISTING credits ledger.
# ---------------------------------------------------------------------------


async def test_ingest_spend_debits_existing_credit_ledger(mongo_db):
    # Seed a wallet via the EXISTING credits service (the ledger we plug into).
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await provisioning.ensure_tenant_key(WS, budget=BUDGET, admin_client=FakeAdmin())

    # Two proxy spend rows: 0.04 USD (-> 10 credits) + 0.02 USD (-> 5 credits).
    rows = [
        {
            "request_id": "req-1",
            "spend": 0.04,
            "startTime": "2026-06-26T10:00:00",
            "model": "anthropic/claude-3-5-sonnet",
            "prompt_tokens_details": {"cached_tokens": 128},
        },
        {
            "request_id": "req-2",
            "spend": 0.02,
            "startTime": "2026-06-26T10:05:00",
            "model": "anthropic/claude-3-5-sonnet",
        },
    ]
    admin = FakeAdmin(spend_rows=rows)

    result = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    # Debited via the EXISTING ledger: 10 + 5 = 15 credits; balance 1000 -> 985.
    assert result.rows_read == 2
    assert result.rows_billed == 2
    assert result.credits_debited == 15
    assert result.cached_tokens == 128
    assert result.balance_after == 985
    assert await credits.balance(WS) == 985

    # The ledger rows are keyed litellm:{request_id} with the litellm_spend cause —
    # i.e. they live in the SAME CreditLedgerEntry collection BC-3 uses, not a fork.
    spend_entries = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.cause == "litellm_spend",
    ).to_list()
    assert {e.idempotency_key for e in spend_entries} == {"litellm:req-1", "litellm:req-2"}
    by_key = {e.idempotency_key: e for e in spend_entries}
    assert by_key["litellm:req-1"].amount_delta == -10
    assert by_key["litellm:req-1"].ref.get("cached_tokens") == 128
    assert by_key["litellm:req-2"].amount_delta == -5


async def test_reingest_is_idempotent_even_if_high_water_reset(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await provisioning.ensure_tenant_key(WS, budget=BUDGET, admin_client=FakeAdmin())

    rows = [{"request_id": "req-1", "spend": 0.04, "startTime": "2026-06-26T10:00:00"}]
    admin = FakeAdmin(spend_rows=rows)

    first = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)
    assert first.credits_debited == 10
    assert await credits.balance(WS) == 990

    # Force the high-water mark back so the row is re-read — the BC-1 ledger key
    # must still block the second debit (the real exactly-once guard).
    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == WS)
    assert doc is not None
    doc.last_spend_ingest_ts = None
    await doc.save()

    second = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)
    assert second.rows_read == 1  # the row WAS re-read...
    assert second.rows_billed == 0  # ...but produced no NEW ledger debit
    assert await credits.balance(WS) == 990  # balance never moved twice

    # Still exactly one ledger row for that request.
    entries = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.idempotency_key == "litellm:req-1",
    ).to_list()
    assert len(entries) == 1


async def test_high_water_mark_skips_settled_rows(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await provisioning.ensure_tenant_key(WS, budget=BUDGET, admin_client=FakeAdmin())

    admin = FakeAdmin(
        spend_rows=[{"request_id": "req-1", "spend": 0.04, "startTime": "2026-06-26T10:00:00"}]
    )
    await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    # The high-water mark advanced to the row's startTime.
    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == WS)
    assert doc is not None
    assert doc.last_spend_ingest_ts == "2026-06-26T10:00:00"

    # A re-sweep with the SAME (now-settled) row bills NOTHING new. After the WU-F
    # boundary fix the high-water skip is strict ``<`` (not ``<=``), so a row whose
    # startTime EQUALS the mark is RE-EXAMINED (rows_read==1) and de-duplicated by
    # its litellm:{request_id} ledger key — it must not re-bill. (The old ``<=``
    # skipped it outright, rows_read==0, but that same skip silently DROPPED a
    # distinct same-second row on a later sweep — the under-bill this fix closes;
    # see test_high_water_boundary_same_second_rows_both_billed_once.)
    before_balance = await credits.balance(WS)
    again = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)
    assert again.rows_read == 1  # the boundary row is re-examined...
    assert again.rows_billed == 0  # ...but never re-billed (deduped on its key)
    assert await credits.balance(WS) == before_balance  # balance never moved twice

    # A NEWER row gets ingested (the boundary row is re-examined + deduped, the
    # newer row bills).
    admin._spend_rows = [
        {"request_id": "req-1", "spend": 0.04, "startTime": "2026-06-26T10:00:00"},
        {"request_id": "req-2", "spend": 0.08, "startTime": "2026-06-26T11:00:00"},
    ]
    newer = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)
    assert newer.rows_read == 2  # the boundary row (re-examined) + the new row
    assert newer.rows_billed == 1  # only the new row produces a debit
    assert newer.credits_debited == 20  # round(0.08 * 250)
    assert await credits.balance(WS) == 1000 - 10 - 20  # 970


async def test_ingest_unprovisioned_workspace_returns_zero(mongo_db):
    # No key provisioned, no wallet — ingestion is a no-op, not a raise.
    result = await provisioning.ingest_tenant_spend(
        "ws_never_provisioned", spend_card=SPEND, admin_client=FakeAdmin()
    )
    assert result.rows_read == 0
    assert result.rows_billed == 0
    assert result.credits_debited == 0
    assert result.balance_after == 0


async def test_ingest_overage_drives_balance_negative(mongo_db):
    # Tiny wallet: 5 credits, but a 0.04 USD row bills 10 credits.
    await credits.grant(WS, 5, cause="top_up", idempotency_key="seed")
    await provisioning.ensure_tenant_key(WS, budget=BUDGET, admin_client=FakeAdmin())

    admin = FakeAdmin(
        spend_rows=[{"request_id": "req-1", "spend": 0.04, "startTime": "2026-06-26T10:00:00"}]
    )
    result = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    # Metered compute is always billed — the overage is a legitimate negative.
    assert result.credits_debited == 10
    assert result.balance_after == -5
    assert await credits.balance(WS) == -5


# ---------------------------------------------------------------------------
# Config — budgets resolve from settings (never hardcoded secrets).
# ---------------------------------------------------------------------------


def test_load_key_budget_reads_settings(monkeypatch):
    monkeypatch.setenv("POCKETPAW_TENANT_MAX_BUDGET_USD", "50")
    monkeypatch.setenv("POCKETPAW_TENANT_BUDGET_DURATION", "7d")
    monkeypatch.setenv("POCKETPAW_TENANT_RPM_LIMIT", "120")
    monkeypatch.setenv("POCKETPAW_TENANT_TPM_LIMIT", "0")  # 0 -> unset
    from pocketpaw.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        budget = provisioning.load_key_budget()
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]

    assert budget.max_budget_usd == 50.0
    assert budget.budget_duration == "7d"
    assert budget.rpm_limit == 120
    assert budget.tpm_limit is None  # 0 mapped to "no cap"


def test_spend_ingest_disabled_by_default(monkeypatch):
    monkeypatch.delenv("POCKETPAW_LITELLM_SPEND_INGEST", raising=False)
    from pocketpaw.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        assert provisioning.spend_ingest_enabled() is False
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]
