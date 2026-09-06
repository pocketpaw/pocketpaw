# tests/cloud/llm_provisioning/test_run_end_trigger.py — proves a run's cost is
# billed shortly after it ends, not up to five minutes later.
#
# THE GAP. In ``live`` mode the LiteLLM cutover sweep is the only meter, and it
# runs on a five-minute heartbeat. So a customer's balance lagged their usage by
# up to that long, and the run-start balance gate could admit a run the previous
# one had already spent the credits for.
#
# WHAT THESE TESTS PIN. Not the ingest itself (that is covered next door) but the
# properties that make triggering it from the run lifecycle safe:
#
#   * it only fires in ``live`` — in ``off`` / ``shadow`` the per-run meter is
#     still charging, and running both would bill the same compute twice under two
#     different idempotency keys;
#   * it waits before reading, because the proxy writes its spend row from a
#     background task AFTER the response returns (measured at ~15s on the
#     production gateway, absent at 6s) so an immediate read finds nothing;
#   * it can never fail or delay the run that triggered it;
#   * and racing the sweep is harmless — both bill the same row and the ledger's
#     unique key means the second is a no-op.
#
# Created 2026-09-04 (feat/bill-spend-at-run-end): new test module.

from __future__ import annotations

import asyncio

import pytest
from pocketpaw_ee.cloud.credits import service as credits
from pocketpaw_ee.cloud.credits.domain import credits_to_micro
from pocketpaw_ee.cloud.llm_provisioning import run_end_trigger
from pocketpaw_ee.cloud.llm_provisioning import service as provisioning
from pocketpaw_ee.cloud.llm_provisioning.domain import KeyBudget, SpendCredits
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey

WS = "ws_run_end_trigger"
SPEND = SpendCredits(markup=2.5, credit_usd=0.01)

# One real-shaped proxy row: $0.0015, which is 375_000 micro-credits.
ROW = {
    "request_id": "req-run-end",
    "spend": 0.0015,
    "startTime": "2026-09-04T10:00:00",
    "model": "gpt-5.2-mini",
}
ROW_MICRO = 375_000


class FakeAdmin:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.reads = 0

    async def generate_key(self, **kwargs):
        return {"key": f"sk-{kwargs.get('key_alias', 'x')}", **kwargs}

    async def spend_logs(self, *, api_key: str):
        self.reads += 1
        return list(self.rows)

    async def spend_logs_by_end_user(self, *, end_user, start_date, end_date, page_size=100):
        return []

    async def spend_log_count(self, *, start_date, end_date, end_user=None):
        return 0

    async def list_customers(self):
        return []


@pytest.fixture(autouse=True)
def _fast_and_enabled(monkeypatch):
    """No real waiting, and the trigger explicitly on."""
    monkeypatch.setenv("POCKETPAW_SPEND_TRIGGER_ENABLED", "true")
    monkeypatch.setenv("POCKETPAW_SPEND_TRIGGER_DELAY_SECONDS", "0")


async def _provision() -> LiteLLMTenantKey:
    await provisioning.ensure_tenant_key(WS, budget=KeyBudget(), admin_client=FakeAdmin())
    doc = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == WS)
    assert doc is not None
    return doc


async def test_a_finished_run_bills_without_waiting_for_the_sweep(monkeypatch, mongo_db):
    """The point of the whole thing. The run ends; the charge lands seconds later
    rather than on the next five-minute tick."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(rows=[ROW])
    monkeypatch.setattr(provisioning, "spend_mode", lambda: "live")
    monkeypatch.setattr(provisioning, "LiteLLMAdminClient", lambda: admin)

    task = run_end_trigger.schedule_spend_ingest(WS)
    assert task is not None
    await task

    assert await credits.balance_micro(WS) == credits_to_micro(1000) - ROW_MICRO


async def test_it_does_not_fire_outside_live_mode(monkeypatch, mongo_db):
    """The double-bill boundary. In ``off`` and ``shadow`` the per-run meter is
    still charging; billing proxy spend alongside it would charge the same compute
    twice, under two idempotency keys the ledger cannot recognise as one."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    for mode in ("off", "shadow"):
        monkeypatch.setattr(provisioning, "spend_mode", lambda mode=mode: mode)
        assert run_end_trigger.schedule_spend_ingest(WS) is None

    assert await credits.balance(WS) == 1000


async def test_it_waits_before_reading(monkeypatch, mongo_db):
    """The proxy writes its spend row from a background task after the response is
    sent — measured at ~15s on the production gateway and NOT present at 6s. A
    trigger that read immediately would find an empty window every time and bill
    nothing, which looks exactly like working correctly."""
    monkeypatch.setenv("POCKETPAW_SPEND_TRIGGER_DELAY_SECONDS", "0.05")
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(rows=[ROW])
    monkeypatch.setattr(provisioning, "spend_mode", lambda: "live")
    monkeypatch.setattr(provisioning, "LiteLLMAdminClient", lambda: admin)

    task = run_end_trigger.schedule_spend_ingest(WS)
    assert task is not None
    # Nothing has been read yet — the task is still waiting out its delay.
    assert admin.reads == 0
    await task
    assert admin.reads == 1


async def test_a_failing_ingest_never_escapes(monkeypatch, mongo_db):
    """The run is over and the customer has their answer. A proxy outage must
    reduce this to a missed optimisation, never a failed run — the sweep re-reads
    the same window five minutes later."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    async def _boom(*args, **kwargs):
        raise RuntimeError("proxy is down")

    monkeypatch.setattr(provisioning, "spend_mode", lambda: "live")
    monkeypatch.setattr(provisioning, "ingest_tenant_spend", _boom)

    task = run_end_trigger.schedule_spend_ingest(WS)
    assert task is not None
    await task  # must not raise
    assert task.exception() is None


async def test_the_trigger_and_the_sweep_do_not_double_bill(monkeypatch, mongo_db):
    """They race by design: the trigger fires at ~20s and the sweep every five
    minutes, so both read the same row routinely. Whichever lands first debits and
    the other no-ops on the ledger's unique ``litellm:{request_id}`` key."""
    await credits.grant(WS, 1000, cause="top_up", idempotency_key="seed")
    await _provision()

    admin = FakeAdmin(rows=[ROW])
    monkeypatch.setattr(provisioning, "spend_mode", lambda: "live")
    monkeypatch.setattr(provisioning, "LiteLLMAdminClient", lambda: admin)

    await asyncio.gather(
        run_end_trigger.schedule_spend_ingest(WS),
        provisioning.ingest_tenant_spend(WS, spend_card=SPEND, admin_client=admin),
    )

    assert await credits.balance_micro(WS) == credits_to_micro(1000) - ROW_MICRO


async def test_the_kill_switch_works(monkeypatch, mongo_db):
    """This adds proxy reads proportional to run volume rather than to time. An
    operator who finds that expensive needs to stop it without a deploy."""
    monkeypatch.setenv("POCKETPAW_SPEND_TRIGGER_ENABLED", "false")
    monkeypatch.setattr(provisioning, "spend_mode", lambda: "live")

    assert run_end_trigger.is_enabled() is False
    assert run_end_trigger.schedule_spend_ingest(WS) is None


async def test_a_workspaceless_run_schedules_nothing(monkeypatch, mongo_db):
    """A run outside a cloud dispatch has no workspace to bill, and an empty id
    would be a read for a tenant that does not exist."""
    monkeypatch.setattr(provisioning, "spend_mode", lambda: "live")
    assert run_end_trigger.schedule_spend_ingest("") is None
