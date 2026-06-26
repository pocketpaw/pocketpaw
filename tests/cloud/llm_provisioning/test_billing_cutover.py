# tests/cloud/llm_provisioning/test_billing_cutover.py — proves the WU-F billing
# cutover from BC-3 per-run metering to LiteLLM as the SINGLE meter, via the safe
# shadow-compare phase. This touches MONEY, so every mode is proven at the ledger:
#
# SHADOW (the safe compare — ZERO debits):
#   * reconcile_tenant_spend records a reconciliation row with the right
#     litellm/bc3/delta and the coverage_gap verdict, AND leaves the credit ledger
#     COMPLETELY UNCHANGED (the critical invariant — shadow never debits).
#   * a coverage-gap case (LiteLLM spend >> BC-3) flips coverage_gap=True.
#   * the cutover sweep in shadow mode reconciles every provisioned tenant and
#     still moves no money.
#
# LIVE (LiteLLM is the sole meter — exactly ONE meter charges):
#   * the BC-3 metering sweep (sweep_unbilled_runs) is GATED OFF in live mode:
#     it returns 0, debits nothing, and does NOT flip the run's billed flag.
#   * the cutover sweep in live mode debits the proxy spend exactly once.
#   * end-to-end: with the same usage present as a chat run AND a proxy spend row,
#     live mode charges it once (LiteLLM), never twice.
#
# MODE TRANSITIONS + BACK-COMPAT:
#   * effective_spend_mode resolves off/shadow/live and honours the legacy
#     POCKETPAW_LITELLM_SPEND_INGEST bool (True -> live while mode is 'off').
#
# BOUNDARY FIX (WU-C high-water under-bill regression):
#   * two DISTINCT spend rows sharing one startTime, arriving across two sweeps,
#     are both billed EXACTLY once (the old start_ts <= high_water dropped the
#     second one -> under-bill).
#
# Uses the shared ``mongo_db`` + autouse ``recording_bus`` fixtures from
# tests/cloud/conftest.py. A FAKE admin client stands in for the proxy.
#
# Created 2026-06-26 (feat/litellm-billing-cutover, WU-F): new test module.

from __future__ import annotations

from datetime import UTC, datetime

from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.llm_provisioning import cutover_sweeper
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning
from pocketpaw_ee.cloud.llm_provisioning.domain import KeyBudget, SpendCredits
from pocketpaw_ee.cloud.metering.domain import RateCard
from pocketpaw_ee.cloud.metering.sweeper import sweep_unbilled_runs
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry
from pocketpaw_ee.cloud.models.spend_reconciliation import SpendReconciliation

WS = "ws_cutover_test"

# Pin both rate cards so credits don't depend on ambient settings: round(usd*250).
SPEND = SpendCredits(markup=2.5, credit_usd=0.01)
RATE = RateCard(markup=2.5, credit_usd=0.01)


class FakeAdmin:
    """In-memory stand-in for LiteLLMAdminClient (no HTTP).

    ``generate_key`` mints a deterministic key; ``spend_logs`` returns scripted
    rows. ``raise_on_generate`` simulates a proxy-down provision failure.
    """

    def __init__(
        self, *, spend_rows: list[dict] | None = None, raise_on_generate: bool = False
    ) -> None:
        self.generate_calls = 0
        self._spend_rows = spend_rows or []
        self._raise_on_generate = raise_on_generate

    async def generate_key(self, **kwargs):
        self.generate_calls += 1
        if self._raise_on_generate:
            from pocketpaw_ee.catalog.admin_client import LiteLLMAdminError

            raise LiteLLMAdminError("proxy unreachable (simulated)")
        return {"key": f"sk-{kwargs.get('key_alias', 'x')}", **kwargs}

    async def spend_logs(self, *, api_key: str):
        return list(self._spend_rows)


async def _make_run(
    *, run_id: str, status: str = "completed", usage: dict | None = None, billed: bool = False
) -> ChatRunDoc:
    doc = ChatRunDoc(
        run_id=run_id,
        workspace=WS,
        context_type="dm",
        scope_id="scope-1",
        session_key="sk-1",
        user_id="u1",
        agent_id="a1",
        client_message_id=f"cmid-{run_id}",
        user_message_id=f"umid-{run_id}",
        status=status,  # type: ignore[arg-type]
        usage=usage if usage is not None else {},
        billed=billed,
    )
    await doc.insert()
    return doc


async def _ledger_snapshot(workspace: str) -> tuple[int, int]:
    """(#ledger entries, balance) — the two things shadow must NOT change."""
    entries = await CreditLedgerEntry.find(CreditLedgerEntry.workspace == workspace).to_list()
    return len(entries), await credits.balance(workspace)


# ===========================================================================
# SHADOW — the safe compare. ZERO debits. Reconciliation record produced.
# ===========================================================================


async def test_shadow_records_reconciliation_and_never_debits(mongo_db):
    # Seed a wallet and a BC-3 compute_spend debit (what BC-3 already billed).
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    # BC-3 billed 10 credits of compute for this workspace.
    await credits.debit(
        WS, 10, cause="compute_spend", idempotency_key="run:r1", allow_negative=True
    )
    await provisioning.ensure_tenant_key(WS, budget=KeyBudget(), admin_client=FakeAdmin())

    # The proxy attributes 0.04 USD == 10 credits for the SAME window -> they agree.
    admin = FakeAdmin(
        spend_rows=[{"request_id": "req-1", "spend": 0.04, "startTime": "2026-06-26T10:00:00"}]
    )

    before_entries, before_balance = await _ledger_snapshot(WS)

    rec = await provisioning.reconcile_tenant_spend(
        WS, spend_card=SPEND, threshold=2, admin_client=admin
    )

    # The compare: litellm 10 == bc3 10, delta 0, no gap.
    assert rec.litellm_credits == 10
    assert rec.bc3_credits == 10
    assert rec.delta == 0
    assert rec.coverage_gap is False
    assert rec.litellm_rows == 1
    assert rec.bc3_entries == 1

    # CRITICAL — shadow performed ZERO debits: ledger entry count AND balance are
    # byte-for-byte unchanged (no litellm_spend row was written).
    after_entries, after_balance = await _ledger_snapshot(WS)
    assert after_entries == before_entries
    assert after_balance == before_balance
    litellm_debits = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.cause == "litellm_spend",
    ).to_list()
    assert litellm_debits == []  # shadow NEVER writes a litellm_spend debit

    # The reconciliation record was persisted (the audit trail).
    records = await SpendReconciliation.find(SpendReconciliation.workspace == WS).to_list()
    assert len(records) == 1
    assert records[0].litellm_credits == 10
    assert records[0].bc3_credits == 10
    assert records[0].delta == 0
    assert records[0].coverage_gap is False


async def test_shadow_coverage_gap_when_litellm_exceeds_bc3(mongo_db):
    # BC-3 billed only 2 credits, but the proxy saw 0.40 USD == 100 credits of
    # spend -> a coverage gap (traffic bypassing per-run metering).
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await credits.debit(WS, 2, cause="compute_spend", idempotency_key="run:r1", allow_negative=True)
    await provisioning.ensure_tenant_key(WS, budget=KeyBudget(), admin_client=FakeAdmin())

    admin = FakeAdmin(
        spend_rows=[{"request_id": "req-1", "spend": 0.40, "startTime": "2026-06-26T10:00:00"}]
    )

    before_entries, before_balance = await _ledger_snapshot(WS)

    rec = await provisioning.reconcile_tenant_spend(
        WS, spend_card=SPEND, threshold=10, admin_client=admin
    )

    assert rec.litellm_credits == 100
    assert rec.bc3_credits == 2
    assert rec.delta == 98  # litellm - bc3
    assert rec.coverage_gap is True  # |98| > 10

    # Still ZERO debits — even a gap only RECORDS, never charges.
    after_entries, after_balance = await _ledger_snapshot(WS)
    assert after_entries == before_entries
    assert after_balance == before_balance

    records = await SpendReconciliation.find(SpendReconciliation.workspace == WS).to_list()
    assert len(records) == 1
    assert records[0].coverage_gap is True
    assert records[0].delta == 98


async def test_shadow_window_filters_bc3_and_litellm(mongo_db):
    # A BC-3 debit and a proxy row INSIDE the window count; ones outside don't.
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await provisioning.ensure_tenant_key(WS, budget=KeyBudget(), admin_client=FakeAdmin())

    # One in-window BC-3 debit (createdAt ~ now), recorded just before the sweep.
    await credits.debit(WS, 5, cause="compute_spend", idempotency_key="run:in", allow_negative=True)

    since = datetime(2000, 1, 1, tzinfo=UTC)  # wide-open past
    until = datetime(2999, 1, 1, tzinfo=UTC)  # wide-open future
    admin = FakeAdmin(
        spend_rows=[
            {"request_id": "req-in", "spend": 0.02, "startTime": "2026-06-26T10:00:00"},  # 5 cr
        ]
    )
    rec = await provisioning.reconcile_tenant_spend(
        WS, since=since, until=until, spend_card=SPEND, threshold=2, admin_client=admin
    )
    assert rec.litellm_credits == 5
    assert rec.bc3_credits == 5
    assert rec.delta == 0
    assert rec.window_start == since.isoformat()
    assert rec.window_end == until.isoformat()


# ===========================================================================
# LIVE — LiteLLM is the sole meter. Exactly ONE meter charges.
# ===========================================================================


async def test_live_gates_bc3_sweep_off(mongo_db):
    # A terminal, unbilled run that BC-3 WOULD bill in off/shadow.
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    run = await _make_run(run_id="run-live", usage={"total_cost_usd": 0.04, "model": "gpt-4o"})
    before = await credits.balance(WS)

    # In LIVE mode the BC-3 sweep MUST NOT charge.
    billed = await sweep_unbilled_runs(rate_card=RATE, mode="live")

    assert billed == 0  # gated off — nothing billed
    assert await credits.balance(WS) == before  # NO debit
    # And it did NOT flip the billed flag (so LiteLLM still owns this usage).
    reloaded = await ChatRunDoc.find_one(ChatRunDoc.run_id == "run-live")
    assert reloaded is not None
    assert reloaded.billed is False
    # No compute_spend ledger row was written.
    bc3 = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.cause == "compute_spend",
    ).to_list()
    assert bc3 == []
    _ = run  # silence unused


async def test_off_and_shadow_modes_still_bill_bc3(mongo_db):
    # Sanity: the gate is LIVE-only. In off + shadow, BC-3 bills as today.
    for mode in ("off", "shadow"):
        ws = f"{WS}_{mode}"
        await credits.grant(ws, 1000, cause="top_up", idempotency_key=f"seed-{mode}")
        doc = ChatRunDoc(
            run_id=f"run-{mode}",
            workspace=ws,
            context_type="dm",
            scope_id="s",
            session_key="sk",
            user_id="u1",
            agent_id="a1",
            client_message_id=f"cmid-{mode}",
            user_message_id=f"umid-{mode}",
            status="completed",  # type: ignore[arg-type]
            usage={"total_cost_usd": 0.04, "model": "gpt-4o"},
            billed=False,
        )
        await doc.insert()

        billed = await sweep_unbilled_runs(rate_card=RATE, mode=mode)
        assert billed == 1, f"BC-3 must bill in {mode} mode"
        assert await credits.balance(ws) == 990, f"BC-3 debit must land in {mode} mode"


async def test_live_cutover_sweep_debits_proxy_spend_once(mongo_db):
    # In live mode the cutover sweep ingests proxy spend exactly once.
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await provisioning.ensure_tenant_key(WS, budget=KeyBudget(), admin_client=FakeAdmin())

    rows = [{"request_id": "req-1", "spend": 0.04, "startTime": "2026-06-26T10:00:00"}]

    # Patch the admin client the service constructs (the sweep calls
    # ingest_tenant_spend without an injected client), and pin the mode.
    import pocketpaw_ee.cloud.llm_provisioning.service as svc

    orig = svc.LiteLLMAdminClient
    svc.LiteLLMAdminClient = lambda *a, **k: FakeAdmin(spend_rows=rows)  # type: ignore[assignment]
    # Pin the rate card the same way so credits are deterministic.
    orig_load = svc.load_spend_credits
    svc.load_spend_credits = lambda: SPEND  # type: ignore[assignment]
    try:
        first = await cutover_sweeper.run_cutover_sweep(mode="live")
        assert first["processed"] == 1
        assert first["credits"] == 10
        assert await credits.balance(WS) == 990  # 1000 - 10

        # Re-run: the ledger key + high-water mark make it a no-op.
        second = await cutover_sweeper.run_cutover_sweep(mode="live")
        assert second["credits"] == 0
        assert await credits.balance(WS) == 990  # never moved twice
    finally:
        svc.LiteLLMAdminClient = orig  # type: ignore[assignment]
        svc.load_spend_credits = orig_load  # type: ignore[assignment]

    # Exactly one litellm_spend ledger row.
    entries = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS,
        CreditLedgerEntry.idempotency_key == "litellm:req-1",
    ).to_list()
    assert len(entries) == 1
    assert entries[0].amount_delta == -10


async def test_no_double_charge_same_usage_live(mongo_db):
    # The single-meter guarantee end-to-end: the SAME unit of usage exists both as
    # a terminal chat run (BC-3's input) AND as a proxy spend row (LiteLLM's input).
    # In live mode it must be charged EXACTLY once, by LiteLLM only.
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await provisioning.ensure_tenant_key(WS, budget=KeyBudget(), admin_client=FakeAdmin())
    await _make_run(run_id="run-x", usage={"total_cost_usd": 0.04, "model": "gpt-4o"})

    rows = [{"request_id": "req-x", "spend": 0.04, "startTime": "2026-06-26T10:00:00"}]
    import pocketpaw_ee.cloud.llm_provisioning.service as svc

    orig = svc.LiteLLMAdminClient
    orig_load = svc.load_spend_credits
    svc.LiteLLMAdminClient = lambda *a, **k: FakeAdmin(spend_rows=rows)  # type: ignore[assignment]
    svc.load_spend_credits = lambda: SPEND  # type: ignore[assignment]
    try:
        # BC-3 sweep (gated off in live) + cutover sweep (live ingest).
        bc3_billed = await sweep_unbilled_runs(rate_card=RATE, mode="live")
        cut = await cutover_sweeper.run_cutover_sweep(mode="live")
    finally:
        svc.LiteLLMAdminClient = orig  # type: ignore[assignment]
        svc.load_spend_credits = orig_load  # type: ignore[assignment]

    assert bc3_billed == 0  # BC-3 charged nothing
    assert cut["credits"] == 10  # LiteLLM charged the 10 credits

    # Net: exactly 10 credits charged total (1000 -> 990), via litellm_spend only.
    assert await credits.balance(WS) == 990
    bc3_rows = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS, CreditLedgerEntry.cause == "compute_spend"
    ).to_list()
    litellm_rows = await CreditLedgerEntry.find(
        CreditLedgerEntry.workspace == WS, CreditLedgerEntry.cause == "litellm_spend"
    ).to_list()
    assert bc3_rows == []  # BC-3 never charged this usage
    assert len(litellm_rows) == 1  # LiteLLM charged it once


# ===========================================================================
# CUTOVER SWEEP — off no-ops; shadow reconciles every tenant, moves no money.
# ===========================================================================


async def test_cutover_sweep_off_is_noop(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await provisioning.ensure_tenant_key(WS, budget=KeyBudget(), admin_client=FakeAdmin())
    before = await credits.balance(WS)

    summary = await cutover_sweeper.run_cutover_sweep(mode="off")

    assert summary["processed"] == 0
    assert await credits.balance(WS) == before
    assert await SpendReconciliation.find(SpendReconciliation.workspace == WS).to_list() == []


async def test_cutover_sweep_shadow_reconciles_tenants_without_debit(mongo_db):
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await credits.debit(
        WS, 10, cause="compute_spend", idempotency_key="run:r1", allow_negative=True
    )
    await provisioning.ensure_tenant_key(WS, budget=KeyBudget(), admin_client=FakeAdmin())

    rows = [{"request_id": "req-1", "spend": 0.04, "startTime": datetime.now(UTC).isoformat()}]
    import pocketpaw_ee.cloud.llm_provisioning.service as svc

    orig = svc.LiteLLMAdminClient
    orig_load = svc.load_spend_credits
    svc.LiteLLMAdminClient = lambda *a, **k: FakeAdmin(spend_rows=rows)  # type: ignore[assignment]
    svc.load_spend_credits = lambda: SPEND  # type: ignore[assignment]
    before_entries, before_balance = await _ledger_snapshot(WS)
    try:
        summary = await cutover_sweeper.run_cutover_sweep(mode="shadow")
    finally:
        svc.LiteLLMAdminClient = orig  # type: ignore[assignment]
        svc.load_spend_credits = orig_load  # type: ignore[assignment]

    assert summary["processed"] == 1
    # ZERO debits even via the sweep path.
    after_entries, after_balance = await _ledger_snapshot(WS)
    assert after_entries == before_entries
    assert after_balance == before_balance
    # A reconciliation row was recorded for the tenant.
    assert len(await SpendReconciliation.find(SpendReconciliation.workspace == WS).to_list()) == 1


# ===========================================================================
# MODE TRANSITIONS + BACK-COMPAT of the legacy POCKETPAW_LITELLM_SPEND_INGEST bool.
# ===========================================================================


def _mode_for(monkeypatch, *, mode_env: str | None, ingest_env: str | None) -> str:
    from pocketpaw.config import get_settings

    if mode_env is None:
        monkeypatch.delenv("POCKETPAW_LITELLM_SPEND_MODE", raising=False)
    else:
        monkeypatch.setenv("POCKETPAW_LITELLM_SPEND_MODE", mode_env)
    if ingest_env is None:
        monkeypatch.delenv("POCKETPAW_LITELLM_SPEND_INGEST_ENABLED", raising=False)
    else:
        monkeypatch.setenv("POCKETPAW_LITELLM_SPEND_INGEST_ENABLED", ingest_env)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        return provisioning.spend_mode()
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_mode_default_is_off(monkeypatch):
    assert _mode_for(monkeypatch, mode_env=None, ingest_env=None) == "off"


def test_legacy_ingest_bool_maps_to_shadow_not_live(monkeypatch):
    # MONEY SAFETY: an old deployment that set only the legacy bool must resolve to
    # 'shadow' (read-only compare, ZERO debits) — NEVER 'live'. Deploying WU-F (which
    # adds the first periodic ingestion caller) must not auto-start LiteLLM billing.
    assert _mode_for(monkeypatch, mode_env=None, ingest_env="true") == "shadow"


def test_live_is_never_inferred_from_legacy_bool(monkeypatch):
    # 'live' requires an EXPLICIT POCKETPAW_LITELLM_SPEND_MODE=live. The legacy bool
    # alone can only ever reach 'shadow' — there is no value of the bool that yields
    # 'live'. This is the core anti-silent-flip guarantee.
    assert _mode_for(monkeypatch, mode_env=None, ingest_env="true") == "shadow"
    assert _mode_for(monkeypatch, mode_env="off", ingest_env="true") == "shadow"
    # The ONLY path to live is the explicit mode.
    assert _mode_for(monkeypatch, mode_env="live", ingest_env=None) == "live"


def test_explicit_shadow_overrides_legacy_bool(monkeypatch):
    # A new explicit 'shadow' is taken as-is alongside a stale bool.
    assert _mode_for(monkeypatch, mode_env="shadow", ingest_env="true") == "shadow"


def test_explicit_live_overrides_legacy_bool(monkeypatch):
    # An explicit 'live' wins (the operator consciously chose to bill), bool or not.
    assert _mode_for(monkeypatch, mode_env="live", ingest_env="true") == "live"


def test_explicit_modes_resolve(monkeypatch):
    assert _mode_for(monkeypatch, mode_env="off", ingest_env=None) == "off"
    assert _mode_for(monkeypatch, mode_env="shadow", ingest_env=None) == "shadow"
    assert _mode_for(monkeypatch, mode_env="live", ingest_env=None) == "live"


def test_legacy_bool_emits_one_time_deprecation_warning(monkeypatch, caplog):
    # When the legacy bool is the only signal, the first mode resolution logs a
    # one-time deprecation notice telling the operator it now means 'shadow' and
    # that billing needs an explicit mode. Fires at most once per process.
    import logging

    import pocketpaw_ee.cloud.llm_provisioning.service as svc

    from pocketpaw.config import get_settings

    monkeypatch.setattr(svc, "_legacy_bool_warned", False)  # reset the once-guard
    monkeypatch.delenv("POCKETPAW_LITELLM_SPEND_MODE", raising=False)
    monkeypatch.setenv("POCKETPAW_LITELLM_SPEND_INGEST_ENABLED", "true")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        with caplog.at_level(logging.WARNING, logger=svc.logger.name):
            assert svc.spend_mode() == "shadow"
            # Resolve again — the warning must NOT fire a second time.
            assert svc.spend_mode() == "shadow"
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]

    dep = [r for r in caplog.records if "DEPRECATION (WU-F)" in r.getMessage()]
    assert len(dep) == 1, "the deprecation notice must fire exactly once"
    msg = dep[0].getMessage()
    assert "'shadow'" in msg
    assert "POCKETPAW_LITELLM_SPEND_MODE=live" in msg


def test_no_warning_when_mode_explicit(monkeypatch, caplog):
    # With an explicit mode set, the legacy-bool notice never fires (the operator
    # already made a conscious choice).
    import logging

    import pocketpaw_ee.cloud.llm_provisioning.service as svc

    from pocketpaw.config import get_settings

    monkeypatch.setattr(svc, "_legacy_bool_warned", False)
    monkeypatch.setenv("POCKETPAW_LITELLM_SPEND_MODE", "live")
    monkeypatch.setenv("POCKETPAW_LITELLM_SPEND_INGEST_ENABLED", "true")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        with caplog.at_level(logging.WARNING, logger=svc.logger.name):
            assert svc.spend_mode() == "live"
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]

    assert not [r for r in caplog.records if "DEPRECATION (WU-F)" in r.getMessage()]


def test_spend_ingest_enabled_shim_true_only_in_live(monkeypatch):
    # The deprecated shim: True ONLY when the effective mode is live.
    assert _mode_for(monkeypatch, mode_env="off", ingest_env=None) == "off"
    from pocketpaw.config import get_settings

    monkeypatch.setenv("POCKETPAW_LITELLM_SPEND_MODE", "shadow")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        assert provisioning.spend_ingest_enabled() is False  # shadow does NOT debit
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]
    monkeypatch.setenv("POCKETPAW_LITELLM_SPEND_MODE", "live")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    try:
        assert provisioning.spend_ingest_enabled() is True
    finally:
        get_settings.cache_clear()  # type: ignore[attr-defined]


# ===========================================================================
# BOUNDARY FIX — two distinct same-startTime rows across two sweeps bill once each.
# ===========================================================================


async def test_high_water_boundary_same_second_rows_both_billed_once(mongo_db):
    # The WU-C under-bill bug: with start_ts <= high_water, a DISTINCT row sharing
    # the prior sweep's exact startTime second is dropped on the later sweep. With
    # the strict-< fix + the litellm:{request_id} dedup, BOTH same-second rows bill
    # exactly once across two sweeps.
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await provisioning.ensure_tenant_key(WS, budget=KeyBudget(), admin_client=FakeAdmin())

    SAME_TS = "2026-06-26T10:00:00"

    # Sweep 1 sees only req-1 at SAME_TS (0.04 USD -> 10 credits). Mark advances to
    # SAME_TS.
    admin = FakeAdmin(spend_rows=[{"request_id": "req-1", "spend": 0.04, "startTime": SAME_TS}])
    first = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)
    assert first.rows_billed == 1
    assert first.credits_debited == 10
    assert await credits.balance(WS) == 990

    # Sweep 2: req-2 ALSO carries SAME_TS (a distinct row that arrived/settled at
    # the same second) plus the already-billed req-1. The fix must bill req-2 (the
    # old <= would have skipped it -> under-bill) while req-1 no-ops on its key.
    admin._spend_rows = [
        {"request_id": "req-1", "spend": 0.04, "startTime": SAME_TS},
        {"request_id": "req-2", "spend": 0.06, "startTime": SAME_TS},  # -> 15 credits
    ]
    second = await provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin)

    assert second.rows_billed == 1  # only req-2 is newly billed
    assert second.credits_debited == 15
    assert await credits.balance(WS) == 975  # 990 - 15

    # Exactly one ledger row per request — neither double-billed.
    for rid, delta in (("req-1", -10), ("req-2", -15)):
        entries = await CreditLedgerEntry.find(
            CreditLedgerEntry.workspace == WS,
            CreditLedgerEntry.idempotency_key == f"litellm:{rid}",
        ).to_list()
        assert len(entries) == 1, f"{rid} must have exactly one ledger row"
        assert entries[0].amount_delta == delta
