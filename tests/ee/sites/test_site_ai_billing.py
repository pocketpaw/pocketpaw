# tests/ee/sites/test_site_ai_billing.py: the site AI hard caps and billing.
#
# Covers the atomic reservation (a concurrent burst never commits or settles past a
# cap), settle-to-actual with the surplus released, release when the model never
# started (a refusal, a misconfigured alias, a gate) and the input floor when it did,
# BYOK calls held only against the owner's caps and never debited, the expired-hold
# sweeper, the plan allowance bounding the monthly cap, the per-user cap for Worker
# calls, ``budget_exhausted`` / ``wallet_empty`` as an
# ``unavailable`` stream event (402 for JSON callers, never a 500), a disconnect
# mid-stream settling what streamed, threshold alerts firing once, and the ledger
# ref naming the site with an idempotent ``run:{id}`` key.
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import fakeredis.aioredis
import pytest
from cryptography.fernet import Fernet

from tests.ee.sites.site_ai_harness import (
    OWNER,
    RAN_MODEL,
    WS,
    FakeLlm,
    client,
    failed,
    gateway_token,
    make_pocket,
    make_site,
    ok,
    parse_sse,
    text_events,
)

MICRO = 1_000_000


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from pocketpaw_ee.cloud._core import redis_client
    from pocketpaw_ee.site_ai import aliases, chat

    from pocketpaw.security.rate_limiter import RateLimiter

    monkeypatch.setattr(aliases, "_OFFLINE", {})

    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    monkeypatch.setattr(chat, "_ip_limiter", RateLimiter(rate=1000.0, capacity=1000))
    monkeypatch.setattr(chat, "_schedule_bill", lambda run_id: None)


async def _setup(plan="staff", **budget):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiBudgetDoc

    pid = await make_pocket()
    site = await make_site(pid, plan_tier=plan)
    if budget:
        await SiteAiBudgetDoc(workspace=WS, pocket_id=pid, **budget).insert()
    return site, await gateway_token(site)


async def _counter(site, scope, period=None):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCounterDoc
    from pocketpaw_ee.site_ai.service import counter_key, day_period, month_period

    period = period or (month_period() if scope == "month" else day_period())
    return await SiteAiCounterDoc.find_one(
        SiteAiCounterDoc.key == counter_key(WS, site.pocket_id, scope, period)
    )


def _body(**extra):
    return {"model": "fast", "messages": [{"role": "user", "content": "hi"}], **extra}


async def _post(c, site, token, body):
    return await c.post(
        f"/api/v1/sites/{site.id}/ai/chat",
        json=body,
        headers={"authorization": f"Bearer {token}"},
    )


def _hold_for(max_tokens: int) -> int:
    from pocketpaw_ee.site_ai import aliases, validate

    est = validate.estimate_tokens([{"role": "user", "content": "hi"}])
    return aliases.reserve_micro("fast", None, est, max_tokens)


def _micro(tokens_in: int, tokens_out: int, model: str | None = RAN_MODEL) -> int:
    from pocketpaw_ee.site_ai import aliases

    usage = {"input_tokens": tokens_in, "output_tokens": tokens_out}
    return aliases.to_micro(aliases.cost_usd("fast", model, usage, (tokens_in, tokens_out)))


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [True, False])
async def test_concurrent_burst_never_passes_the_daily_cap(beanie_test_db, monkeypatch, stream):
    hold = _hold_for(1000)
    cap = hold * 3 + hold // 2  # room for exactly three holds
    site, token = await _setup(daily_cap_micro=cap)
    seen: list[int] = []

    fake = FakeLlm(monkeypatch, script=text_events("ok", 9, 900))
    fake.gate = asyncio.Event()
    original_stream, original_complete = fake._stream, fake._complete

    async def _observe():
        doc = await _counter(site, "day")
        seen.append(doc.committed_micro)

    async def _stream(**kw):
        await _observe()
        async for ev in original_stream(**kw):
            yield ev

    async def _complete(**kw):
        await _observe()
        return await original_complete(**kw)

    from pocketpaw_ee.cloud.chat import side_completion

    monkeypatch.setattr(side_completion, "agent_stream", _stream)
    monkeypatch.setattr(side_completion, "agent_complete", _complete)

    async def _release_later():
        await asyncio.sleep(0.5)
        fake.gate.set()

    async with client() as c:
        releaser = asyncio.create_task(_release_later())
        results = await asyncio.gather(
            *(
                _post(c, site, token, _body(max_output_tokens=1000, stream=stream))
                for _ in range(20)
            )
        )
        await releaser
    if stream:
        ok = [r for r in results if parse_sse(r.text)[-1][0] == "done"]
        limited = [
            r for r in results if parse_sse(r.text) == [("unavailable", {"reason": "limit"})]
        ]
    else:
        ok = [r for r in results if r.status_code == 200]
        limited = [
            r for r in results if r.status_code == 402 and r.json()["detail"] == "budget_exhausted"
        ]
    assert len(ok) == 3 and len(limited) == 17
    assert max(seen) <= cap
    doc = await _counter(site, "day")
    assert doc.spent_micro <= cap and doc.committed_micro <= cap
    assert doc.held_micro == 0 and doc.committed_micro == doc.spent_micro
    assert all(r.status_code != 500 for r in results)


@pytest.mark.asyncio
async def test_settle_charges_actual_and_releases_the_surplus(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc

    site, token = await _setup()
    FakeLlm(monkeypatch, script=[text_events("ok", 12, 7)])
    async with client() as c:
        resp = await _post(c, site, token, _body())
    usage = parse_sse(resp.text)[-2][1]
    actual = _micro(12, 7)
    assert usage["credits_micro"] == actual
    call = await SiteAiCallDoc.find_one(SiteAiCallDoc.state == "settled")
    assert call.reserved_micro > actual and call.actual_micro == actual
    for scope in ("day", "month"):
        doc = await _counter(site, scope)
        assert (doc.committed_micro, doc.held_micro, doc.spent_micro) == (actual, 0, actual)


@pytest.mark.asyncio
async def test_budget_exhausted_streams_unavailable_and_json_gets_402(beanie_test_db, monkeypatch):
    site, token = await _setup(daily_cap_micro=10)
    fake = FakeLlm(monkeypatch, script=[text_events()])
    async with client() as c:
        streamed = await _post(c, site, token, _body())
        plain = await _post(c, site, token, _body(stream=False))
    assert streamed.status_code == 200
    assert parse_sse(streamed.text) == [("unavailable", {"reason": "limit"})]
    assert (plain.status_code, plain.json()["detail"]) == (402, "budget_exhausted")
    assert fake.payloads == []
    doc = await _counter(site, "day")
    assert doc.committed_micro == 0 and doc.held_micro == 0


@pytest.mark.asyncio
async def test_wallet_empty_streams_unavailable_and_json_gets_402(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud._core.errors import InsufficientCredits
    from pocketpaw_ee.cloud.credits import guards

    async def _empty(workspace_id, *a, **kw):
        assert workspace_id == WS
        return InsufficientCredits(requested=1, available=0)

    monkeypatch.setattr(guards, "over_billing_limit", _empty)
    site, token = await _setup()
    fake = FakeLlm(monkeypatch, script=[text_events()])
    async with client() as c:
        streamed = await _post(c, site, token, _body())
        plain = await _post(c, site, token, _body(stream=False))
    assert parse_sse(streamed.text) == [("unavailable", {"reason": "limit"})]
    assert (plain.status_code, plain.json()["detail"]) == (402, "wallet_empty")
    assert fake.payloads == []


@pytest.mark.asyncio
async def test_plan_allowance_bounds_the_monthly_cap(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCounterDoc
    from pocketpaw_ee.site_ai.service import counter_key, month_period

    # The owner allows 1,000 credits a month; the ``site`` plan allows 500.
    site, token = await _setup(plan="site", monthly_cap_micro=1_000 * MICRO)
    await SiteAiCounterDoc(
        key=counter_key(WS, site.pocket_id, "month", month_period()),
        workspace=WS,
        pocket_id=site.pocket_id,
        committed_micro=500 * MICRO - 10,
        spent_micro=500 * MICRO - 10,
    ).insert()
    FakeLlm(monkeypatch, script=[text_events()])
    async with client() as c:
        resp = await _post(c, site, token, _body())
    assert parse_sse(resp.text) == [("unavailable", {"reason": "limit"})]


@pytest.mark.asyncio
async def test_worker_user_gets_the_per_visitor_cap(beanie_test_db, monkeypatch):
    hold = _hold_for(500)
    site, token = await _setup(per_visitor_daily_micro=hold + hold // 2)
    FakeLlm(monkeypatch, script=[text_events("ok", 9, 400)])
    async with client() as c:
        first = await _post(c, site, token, _body(user="u1", max_output_tokens=500))
        second = await _post(c, site, token, _body(user="u1", max_output_tokens=500))
        other = await _post(c, site, token, _body(user="u2", max_output_tokens=500))
        anon = await _post(c, site, token, _body(max_output_tokens=500))
    assert parse_sse(first.text)[-1][0] == "done"
    assert parse_sse(second.text) == [("unavailable", {"reason": "limit"})]
    assert parse_sse(other.text)[-1][0] == "done"
    assert parse_sse(anon.text)[-1][0] == "done"


@pytest.mark.asyncio
async def test_a_backend_error_bills_what_it_reported_and_nothing_without_usage(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc

    site, token = await _setup()
    usage = {"input_tokens": 30, "output_tokens": 0}
    FakeLlm(monkeypatch, script=[failed("error", "agent.backend_error", sent=True, usage=usage)])
    async with client() as c:
        await _post(c, site, token, _body())
        await _post(c, site, token, _body(stream=False))
    rows = await SiteAiCallDoc.find(SiteAiCallDoc.state == "settled").to_list()
    assert len(rows) == 2 and all(r.input_tokens == 30 for r in rows)

    # No usage and no output: the backend refused before doing any work.
    bare, bare_token = await _setup()
    FakeLlm(monkeypatch, script=[failed("error", "agent.backend_error", sent=True)])
    async with client() as c:
        await _post(c, bare, bare_token, _body())
    doc = await _counter(bare, "day")
    assert (doc.spent_micro, doc.held_micro) == (0, 0)


@pytest.mark.asyncio
async def test_a_call_the_model_never_started_costs_nothing(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc

    site, token = await _setup()
    FakeLlm(monkeypatch, script=[failed("error", "side.unexpected", sent=False)])
    async with client() as c:
        await _post(c, site, token, _body())
    assert await SiteAiCallDoc.find(SiteAiCallDoc.state == "released").count() == 1
    doc = await _counter(site, "day")
    assert (doc.committed_micro, doc.held_micro, doc.spent_micro) == (0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("model", "model.not_available"),  # the production failure: no such model
        ("model", "byok.model_provider_mismatch"),
        ("no_agent", "agent.not_found"),
        ("billing", "credits.insufficient"),
        ("error", "byok.base_url_rejected"),
        ("timeout", "side.timeout"),
    ],
)
async def test_a_refused_or_unstarted_call_costs_nothing(
    beanie_test_db, monkeypatch, failure, code
):
    """The model never started (a gate, a misconfigured alias, a refusal): nothing
    is billed and every hold is released."""
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc

    site, token = await _setup()
    FakeLlm(monkeypatch, script=[failed(failure, code, sent=False)])
    async with client() as c:
        await _post(c, site, token, _body())
        await _post(c, site, token, _body(stream=False))
    assert await SiteAiCallDoc.find(SiteAiCallDoc.state == "settled").count() == 0
    assert await SiteAiCallDoc.find(SiteAiCallDoc.state == "held").count() == 0
    doc = await _counter(site, "day")
    assert (doc.committed_micro, doc.held_micro, doc.spent_micro) == (0, 0, 0)
    assert await ChatRunDoc.find(ChatRunDoc.context_type == "site_ai").count() == 0


@pytest.mark.asyncio
async def test_a_timeout_after_the_model_started_bills_its_input(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc
    from pocketpaw_ee.site_ai import validate

    site, token = await _setup()
    FakeLlm(monkeypatch, script=[failed("timeout", "side.timeout", sent=True)])
    async with client() as c:
        await _post(c, site, token, _body())
    est = validate.estimate_tokens([{"role": "user", "content": "hi"}])
    rows = await SiteAiCallDoc.find(SiteAiCallDoc.state == "settled").to_list()
    assert len(rows) == 1 and rows[0].input_tokens == est


@pytest.mark.asyncio
async def test_byok_is_held_against_the_owners_caps_only_and_never_debited(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud._core.errors import InsufficientCredits
    from pocketpaw_ee.cloud.credits import guards
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
    from pocketpaw_ee.site_ai import chat

    async def _empty(*_a, **_kw):
        return InsufficientCredits(requested=1, available=0)

    debits, billed = [], []

    async def _debit(**kw):
        debits.append(kw)
        return 0

    monkeypatch.setattr(guards, "over_billing_limit", _empty)  # an empty wallet
    monkeypatch.setattr(credits_service, "debit", _debit)
    monkeypatch.setattr(chat, "_schedule_bill", billed.append)
    site, token = await _setup(plan="site")
    fake = FakeLlm(monkeypatch, script=["ok", ok("ok", 9, 5, payer="byok")], payer="byok")
    async with client() as c:
        resp = await _post(c, site, token, _body())
    assert parse_sse(resp.text)[-1][0] == "done", resp.text
    assert fake.payer_calls == [(WS, OWNER, None)]
    run = await ChatRunDoc.find_one(ChatRunDoc.context_type == "site_ai")
    assert run.credential_source == "byok" and billed == [] and debits == []
    # The owner's own caps still count the call.
    assert (await _counter(site, "day")).spent_micro > 0

    # And the owner's caps still stop it.
    capped, capped_token = await _setup(daily_cap_micro=10)
    async with client() as c:
        refused = await _post(c, capped, capped_token, _body())
    assert parse_sse(refused.text) == [("unavailable", {"reason": "limit"})]


@pytest.mark.asyncio
async def test_sweeper_releases_expired_holds_and_a_late_settle_still_counts(beanie_test_db):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc
    from pocketpaw_ee.site_ai import budget
    from pocketpaw_ee.site_ai.service import day_period

    pid = await make_pocket()
    caps = [budget.Cap("day", day_period(), 1_000)]
    hold = await budget.reserve(
        run_id="r1",
        workspace_id=WS,
        pocket_id=pid,
        site_id="s",
        caller="worker",
        model="fast",
        amount=400,
        caps=caps,
    )
    fresh = await budget.reserve(
        run_id="r2",
        workspace_id=WS,
        pocket_id=pid,
        site_id="s",
        caller="worker",
        model="fast",
        amount=400,
        caps=caps,
    )
    await SiteAiCallDoc.get_pymongo_collection().update_one(
        {"run_id": "r1"}, {"$set": {"expires_at": datetime.now(UTC) - timedelta(minutes=1)}}
    )
    assert await budget.sweep_expired_holds() == 1
    assert await budget.sweep_expired_holds() == 0
    site = type("S", (), {"pocket_id": pid})()
    doc = await _counter(site, "day")
    assert (doc.committed_micro, doc.held_micro) == (400, 400)  # only r2 still held
    await budget.settle(hold, 150, 10, 10)  # r1 finished after all
    await budget.settle(fresh, 100, 10, 10)
    doc = await _counter(site, "day")
    assert (doc.committed_micro, doc.held_micro, doc.spent_micro) == (250, 0, 250)
    await budget.settle(fresh, 100, 10, 10)  # a second settle is a no-op
    doc = await _counter(site, "day")
    assert doc.spent_micro == 250


@pytest.mark.asyncio
async def test_disconnect_mid_stream_settles_what_streamed(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc
    from pocketpaw_ee.site_ai import budget, chat
    from pocketpaw_ee.site_ai.service import day_period

    site, _token = await _setup()
    FakeLlm(monkeypatch, script=["a" * 40, "b", ok("a" * 40 + "b")])
    call = chat.Admitted(
        site=site,
        workspace_id=WS,
        pocket_id=site.pocket_id,
        site_id=str(site.id),
        caller="worker",
        alias="fast",
        owner_id=OWNER,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=100,
        input_estimate=10,
        stream=True,
    )
    call.hold = await budget.reserve(
        run_id=call.run_id,
        workspace_id=WS,
        pocket_id=site.pocket_id,
        site_id=call.site_id,
        caller="worker",
        model="fast",
        amount=10_000,
        caps=[budget.Cap("day", day_period(), 100 * MICRO)],
    )
    gen = chat._events(call)
    first = await gen.__anext__()
    assert first.startswith(b"event: chunk")
    await gen.aclose()
    doc = await SiteAiCallDoc.find_one(SiteAiCallDoc.run_id == call.run_id)
    assert doc.state == "settled" and doc.output_tokens == 40 and doc.input_tokens == 10


@pytest.mark.asyncio
async def test_threshold_alerts_fire_once_per_threshold(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud._core.realtime import emit as emit_mod
    from pocketpaw_ee.cloud.notifications import service as notifications_service
    from pocketpaw_ee.paw_bar import notify

    actual = _micro(9, 400)
    # Two calls cross 50%, the third crosses 80%.
    cap = int(actual * 100 / 55)
    site, token = await _setup(monthly_cap_micro=cap, daily_cap_micro=100 * MICRO)
    events, notes = [], []

    async def _emit(event):
        events.append(event)

    async def _create(**kw):
        notes.append(kw)

    async def _owner(workspace_id):
        return "owner-1"

    monkeypatch.setattr(emit_mod, "emit", _emit)
    monkeypatch.setattr(notifications_service, "create", _create)
    monkeypatch.setattr(notify, "resolve_workspace_owner", _owner)
    FakeLlm(monkeypatch, script=[text_events("ok", 9, 400)])
    async with client() as c:
        await _post(c, site, token, _body(max_output_tokens=400))
    assert [e.data["threshold"] for e in events] == [50]
    assert events[0].type == "site.ai_budget" and events[0].data["owner"] == site.owner
    assert len(notes) == 1 and notes[0]["kind"] == "site_ai_budget"


def test_crossed_picks_the_highest_threshold():
    from pocketpaw_ee.site_ai.alerts import crossed

    assert crossed(49, 100, [50, 80, 100]) == 0
    assert crossed(50, 100, [50, 80, 100]) == 50
    assert crossed(100, 100, [50, 80, 100]) == 100
    assert crossed(5, 0, [50]) == 0


@pytest.mark.asyncio
async def test_site_ai_debit_ref_names_the_site_and_is_keyed_on_the_run(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud.metering import service as metering
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    debits = []

    async def _debit(**kw):
        debits.append(kw)
        return 0

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(metering.credits_service, "debit", _debit)
    monkeypatch.setattr(metering.chat_runs_service, "mark_billed", _noop)
    monkeypatch.setattr(metering, "_reconcile_early_access", _noop)
    doc = ChatRunDoc(
        run_id="run-x",
        workspace=WS,
        context_type="site_ai",
        scope_id="pocket-1",
        session_key="site_ai:s1",
        user_id="site_ai:worker",
        agent_id="site_ai",
        client_message_id="run-x",
        user_message_id="",
        status="completed",
        usage={"total_cost_usd": 0.01, "model": "m", "site_id": "s1", "input_tokens": 3},
    )
    await metering.bill_run(doc)
    await metering.bill_run(doc)
    assert {d["idempotency_key"] for d in debits} == {"run:run-x"}
    ref = debits[0]["ref"]
    assert ref["kind"] == "site_ai" and ref["pocket_id"] == "pocket-1" and ref["site_id"] == "s1"


# ---------------------------------------------------------------- adversarial pass


@pytest.mark.asyncio
async def test_a_broken_wallet_or_hold_read_refuses_without_a_500(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.credits import guards
    from pocketpaw_ee.site_ai import budget

    site, token = await _setup()
    fake = FakeLlm(monkeypatch, script=[text_events()])

    async def _boom(*a, **kw):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(guards, "over_billing_limit", _boom)
    async with client() as c:
        wallet = await _post(c, site, token, _body())
        wallet_json = await _post(c, site, token, _body(stream=False))

    async def _ok(*a, **kw):
        return None

    monkeypatch.setattr(guards, "over_billing_limit", _ok)
    monkeypatch.setattr(budget, "reserve", _boom)
    async with client() as c:
        hold = await _post(c, site, token, _body())
    assert parse_sse(wallet.text) == [("unavailable", {"reason": "temporary"})]
    assert (wallet_json.status_code, wallet_json.json()["detail"]) == (503, "provider_unavailable")
    assert parse_sse(hold.text) == [("unavailable", {"reason": "temporary"})]
    assert fake.payloads == []


@pytest.mark.asyncio
async def test_an_unexpected_json_mode_crash_settles_the_hold(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc

    site, token = await _setup()
    FakeLlm(monkeypatch, result=KeyError("bug"))
    async with client() as c:
        resp = await _post(c, site, token, _body(stream=False))
    assert (resp.status_code, resp.json()["detail"]) == (503, "provider_unavailable")
    # Nothing says the model started, so nothing is billed, and the hold never sticks.
    assert await SiteAiCallDoc.find(SiteAiCallDoc.state == "released").count() == 1


# ---------------------------------------------------------------- hardening review


@pytest.mark.asyncio
async def test_a_counter_past_its_cap_refuses_new_holds(beanie_test_db):
    from pocketpaw_ee.site_ai import budget
    from pocketpaw_ee.site_ai.service import day_period

    pid = await make_pocket()
    caps = [budget.Cap("day", day_period(), 1_000)]
    kw = dict(workspace_id=WS, pocket_id=pid, site_id="s", caller="worker", model="fast")
    hold = await budget.reserve(run_id="a", amount=600, caps=caps, **kw)
    await budget.settle(hold, 1_500, 1, 1)  # real spend passed the cap: kept
    with pytest.raises(budget.BudgetRefused):
        await budget.reserve(run_id="b", amount=1, caps=caps, **kw)


@pytest.mark.asyncio
async def test_sweeper_settles_a_sent_dead_hold_and_debits_it(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc
    from pocketpaw_ee.site_ai import budget
    from pocketpaw_ee.site_ai.service import day_period

    debits = []

    async def _debit(**kw):
        debits.append(kw)
        return 0

    monkeypatch.setattr(credits_service, "debit", _debit)
    pid = await make_pocket()
    caps = [budget.Cap("day", day_period(), 10_000)]
    kw = dict(workspace_id=WS, pocket_id=pid, site_id="s", caller="worker", model="fast")
    await budget.reserve(run_id="sent", amount=400, caps=caps, **kw)
    await budget.mark_sent("sent")
    await budget.reserve(run_id="unsent", amount=300, caps=caps, **kw)
    await SiteAiCallDoc.get_pymongo_collection().update_many(
        {}, {"$set": {"expires_at": datetime.now(UTC) - timedelta(minutes=1)}}
    )
    assert await budget.sweep_expired_holds() == 2
    rows = {r.run_id: r for r in await SiteAiCallDoc.find_all().to_list()}
    assert rows["sent"].state == "settled" and rows["sent"].actual_micro == 400
    assert rows["unsent"].state == "released"
    site = type("S", (), {"pocket_id": pid})()
    doc = await _counter(site, "day")
    assert (doc.committed_micro, doc.held_micro, doc.spent_micro) == (400, 0, 400)
    assert [(d["idempotency_key"], d["amount_micro"]) for d in debits] == [("run:sent", 400)]


def _enforce_credits(monkeypatch, balance: int) -> None:
    from pocketpaw_ee.cloud.credits import guards
    from pocketpaw_ee.cloud.credits import service as credits_service

    from pocketpaw.config import get_settings

    monkeypatch.setattr(get_settings(), "credits_enforced", True, raising=False)

    async def _balance(ws):
        return balance

    async def _ok(*a, **kw):
        return None

    monkeypatch.setattr(credits_service, "balance_micro", _balance)
    monkeypatch.setattr(guards, "over_billing_limit", _ok)


@pytest.mark.asyncio
async def test_wallet_inflight_holds_bound_concurrent_calls_across_sites(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCounterDoc
    from pocketpaw_ee.site_ai import budget

    _enforce_credits(monkeypatch, 1_000)
    cap = await budget.wallet_cap(WS)
    assert cap.limit == 1_000 + budget.WALLET_OVERDRAFT_MICRO
    p1, p2 = await make_pocket(), await make_pocket()
    amount = budget.WALLET_OVERDRAFT_MICRO // 2
    held = []
    for i, pid in enumerate([p1, p2, p1, p2]):
        try:
            held.append(
                await budget.reserve(
                    run_id=f"w{i}",
                    workspace_id=WS,
                    pocket_id=pid,
                    site_id="s",
                    caller="worker",
                    model="fast",
                    amount=amount,
                    caps=[cap],
                )
            )
        except budget.BudgetRefused as exc:
            assert exc.scope == "wallet"
    assert len(held) == 2  # balance + overdraft fits two in-flight holds, not four
    for h in held:
        await budget.settle(h, 5, 1, 1)
    wallet = await SiteAiCounterDoc.find_one(SiteAiCounterDoc.key == budget.wallet_key(WS))
    assert (wallet.committed_micro, wallet.held_micro, wallet.spent_micro) == (0, 0, 0)


@pytest.mark.asyncio
async def test_wallet_hold_refusal_is_wallet_empty(beanie_test_db, monkeypatch):
    from pocketpaw_ee.site_ai import budget

    _enforce_credits(monkeypatch, -budget.WALLET_OVERDRAFT_MICRO)
    site, token = await _setup()
    FakeLlm(monkeypatch, script=[text_events()])
    async with client() as c:
        plain = await _post(c, site, token, _body(stream=False))
    assert (plain.status_code, plain.json()["detail"]) == (402, "wallet_empty")


@pytest.mark.asyncio
async def test_missing_usage_bills_the_estimate(beanie_test_db, monkeypatch):
    from pocketpaw_ee.site_ai import validate

    site, token = await _setup()
    est = validate.estimate_tokens([{"role": "user", "content": "hi"}])
    FakeLlm(monkeypatch, script=["abcd", ok("abcd", usage=None)])
    async with client() as c:
        streamed = await _post(c, site, token, _body())
    FakeLlm(monkeypatch, result=ok("abcdef", usage={"input_tokens": 0, "output_tokens": 0}))
    async with client() as c:
        plain = await _post(c, site, token, _body(stream=False))
    usage = parse_sse(streamed.text)[-2][1]
    assert (usage["input_tokens"], usage["output_tokens"]) == (est, 4)
    assert plain.json()["usage"]["input_tokens"] == est
    assert plain.json()["usage"]["output_tokens"] == 6
    assert plain.json()["usage"]["credits_micro"] > 0


@pytest.mark.asyncio
async def test_a_failed_run_write_still_debits(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.credits import service as credits_service
    from pocketpaw_ee.site_ai import chat

    debits = []

    async def _debit(**kw):
        debits.append(kw)
        return 0

    async def _boom(*a, **kw):
        raise RuntimeError("mongo")

    monkeypatch.setattr(credits_service, "debit", _debit)
    monkeypatch.setattr(chat, "_write_run", _boom)
    site, token = await _setup()
    FakeLlm(monkeypatch, script=[text_events("ok", 9, 5)])
    async with client() as c:
        resp = await _post(c, site, token, _body())
    micro = parse_sse(resp.text)[-2][1]["credits_micro"]
    assert len(debits) == 1 and debits[0]["amount_micro"] == micro
    assert debits[0]["idempotency_key"].startswith("run:")
    assert debits[0]["ref"]["model"] == "site-ai/fast"


@pytest.mark.asyncio
async def test_the_provider_model_never_reaches_owner_spend_views(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.metering import service as metering
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    site, token = await _setup()
    FakeLlm(monkeypatch, script=[text_events("ok", 9, 5)])
    async with client() as c:
        await _post(c, site, token, _body())
    run = await ChatRunDoc.find_one(ChatRunDoc.context_type == "site_ai")
    assert run.usage["model"] == "site-ai/fast" and run.usage["total_cost_usd"] > 0
    debits = []

    async def _debit(**kw):
        debits.append(kw)
        return 0

    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(metering.credits_service, "debit", _debit)
    monkeypatch.setattr(metering.chat_runs_service, "mark_billed", _noop)
    monkeypatch.setattr(metering, "_reconcile_early_access", _noop)
    await metering.bill_run(run)
    assert debits[0]["ref"]["model"] == "site-ai/fast"
    assert "claude" not in str(debits[0]["ref"])


@pytest.mark.asyncio
async def test_live_spend_mode_still_enforces_the_site_caps(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.llm_provisioning import service as provisioning_service

    monkeypatch.setattr(provisioning_service, "spend_mode", lambda: "live")
    site, token = await _setup(daily_cap_micro=10)
    fake = FakeLlm(monkeypatch, script=[text_events()])
    async with client() as c:
        resp = await _post(c, site, token, _body())
    assert parse_sse(resp.text) == [("unavailable", {"reason": "limit"})]
    assert fake.payloads == []


@pytest.mark.asyncio
async def test_a_disconnect_after_the_model_started_bills_its_input(beanie_test_db, monkeypatch):
    """The model call began (``STARTED``) but no token arrived before the caller
    went away: the request reached the model, so it costs its input."""
    from pocketpaw_ee.cloud.chat.side_completion import STARTED
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc
    from pocketpaw_ee.site_ai import budget, chat
    from pocketpaw_ee.site_ai.service import day_period

    site, _token = await _setup()
    FakeLlm(monkeypatch, script=[STARTED, "late", ok("late")])
    call = chat.Admitted(
        site=site,
        workspace_id=WS,
        pocket_id=site.pocket_id,
        site_id=str(site.id),
        caller="worker",
        alias="fast",
        owner_id=OWNER,
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=100,
        input_estimate=10,
        stream=True,
    )
    call.hold = await budget.reserve(
        run_id=call.run_id,
        workspace_id=WS,
        pocket_id=site.pocket_id,
        site_id=call.site_id,
        caller="worker",
        model="fast",
        amount=10_000,
        caps=[budget.Cap("day", day_period(), 100 * MICRO)],
    )
    gen = chat._events(call)
    # Drive the generator until the stream reports the model started, then leave.
    task = asyncio.ensure_future(gen.__anext__())
    for _ in range(50):
        await asyncio.sleep(0)
        if call.sent:
            break
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, StopAsyncIteration):
        pass
    await gen.aclose()
    doc = await SiteAiCallDoc.find_one(SiteAiCallDoc.run_id == call.run_id)
    assert call.sent and doc.state == "settled" and doc.input_tokens == 10


@pytest.mark.asyncio
async def test_the_stream_is_cut_at_the_output_cap(beanie_test_db, monkeypatch):
    site, token = await _setup()
    fake = FakeLlm(monkeypatch, script=["x" * 300, "y" * 300, "z" * 300, ok("never")])
    async with client() as c:
        resp = await _post(c, site, token, _body(max_output_tokens=100))
    events = parse_sse(resp.text)
    # The second chunk passes the cap (600 bytes > 100 tokens at 3 bytes each);
    # the third is never read.
    assert [e for e, _ in events] == ["chunk", "chunk", "usage", "done"]
    assert events[-1][1]["finish_reason"] == "length"
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_the_hold_is_reserved_at_the_higher_of_the_dated_price_and_the_fallback():
    from pocketpaw_ee.site_ai import aliases

    cheap = aliases.reserve_micro("fast", "claude-haiku-5-5", 1000, 4096)
    fallback = aliases.reserve_micro("fast", None, 1000, 4096)
    dear = aliases.reserve_micro("fast", "claude-opus-4-8", 1000, 4096)
    assert cheap == fallback  # a cheap model is never held under the fallback
    assert dear > fallback  # a dear one is held at its own price


@pytest.mark.asyncio
async def test_concurrent_overruns_never_push_spend_past_the_cap(beanie_test_db, monkeypatch):
    """Every call reports usage far above its hold (a backend with no output
    cap): each settles at its hold, so a burst can't overshoot the daily cap."""
    hold = _hold_for(1000)
    cap = hold * 3 + hold // 2
    site, token = await _setup(daily_cap_micro=cap)
    fake = FakeLlm(monkeypatch, script=text_events("ok", 9, 900_000))
    fake.gate = asyncio.Event()

    async def _release_later():
        await asyncio.sleep(0.5)
        fake.gate.set()

    async with client() as c:
        releaser = asyncio.create_task(_release_later())
        await asyncio.gather(
            *(_post(c, site, token, _body(max_output_tokens=1000)) for _ in range(10))
        )
        await releaser
    doc = await _counter(site, "day")
    assert doc.spent_micro <= cap and doc.committed_micro <= cap and doc.held_micro == 0


@pytest.mark.asyncio
async def test_the_payer_the_call_reports_wins(beanie_test_db, monkeypatch):
    """Admission thought BYOK, but the backend ran on the platform's key: the run
    is recorded as platform-paid and goes to ``bill_run``."""
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
    from pocketpaw_ee.site_ai import chat

    billed = []
    monkeypatch.setattr(chat, "_schedule_bill", billed.append)
    site, token = await _setup()
    FakeLlm(monkeypatch, script=["ok", ok("ok", 9, 5, payer="platform")], payer="byok")
    async with client() as c:
        await _post(c, site, token, _body())
    run = await ChatRunDoc.find_one(ChatRunDoc.context_type == "site_ai")
    assert run.credential_source == "platform" and len(billed) == 1
