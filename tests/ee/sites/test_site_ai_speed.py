# tests/ee/sites/test_site_ai_speed.py: site AI latency, prompt-cache stability
# and the size of the billing hold.
#
# Production (2026-11-05, Haiku 5.5 on the Claude SDK backend): almost every call
# wrote its whole prompt to the cache (cache_write ~750-1250 tokens) and the
# CLI's reported cost (Opus rates on a Haiku call) ran up to twice the hold while
# owners were billed from that figure, about 40x the Haiku price. These tests pin:
# pricing from tokens at the verified table rate (never the CLI's figure), a hold
# that covers a prompt written to cache, unpriced models refused, the model pinned
# and logged, ``done`` sent before the billing writes, and a system prompt that
# stays byte-identical per site across calls.
from __future__ import annotations

import logging

import fakeredis.aioredis
import pytest
from cryptography.fernet import Fernet

from tests.ee.sites.site_ai_harness import (
    FakeLlm,
    client,
    gateway_token,
    make_pocket,
    make_site,
    ok,
    parse_sse,
)

SYSTEM = "You are the Crumb & Co. bakery assistant. Answer in two sentences."


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


async def _setup():
    pid = await make_pocket()
    site = await make_site(pid)
    return site, await gateway_token(site)


def _body(text="hi", **extra):
    return {
        "model": "fast",
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
        **extra,
    }


async def _post(c, site, token, body):
    return await c.post(
        f"/api/v1/sites/{site.id}/ai/chat",
        json=body,
        headers={"authorization": f"Bearer {token}"},
    )


# ------------------------------------------------------------------ the hold


def test_the_hold_covers_a_prompt_written_to_the_cache():
    """A Claude SDK call reports its prompt as cache WRITES (billed above the
    plain input rate). The hold must cover a call that writes its whole prompt
    and uses its whole output cap.

    FAILS before the fix: the hold priced the input at the plain input rate."""
    from pocketpaw_ee.site_ai import aliases

    estimate, cap = 3000, 256
    usage = {
        "input_tokens": 2,
        "cache_read_tokens": 0,
        "cache_write_tokens": estimate - 2,
        "output_tokens": cap,
    }
    for model in ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"):
        hold = aliases.reserve_micro("fast", model, estimate, cap)
        cost = aliases.to_micro(aliases.cost_usd("fast", model, usage, (estimate, cap)))
        assert cost <= hold, f"{model}: cost {cost} over hold {hold}"


def test_the_production_call_prices_at_haiku_rates_not_the_clis_figure():
    """Production: in=2, out=22, cache_write=752 on claude-haiku-5-5, and the CLI
    reported total_cost_usd=0.0065918 (Opus 5.5 rates). Site AI prices the tokens
    from the verified table and ignores the CLI's figure.

    FAILS before the fix: the reported cost won (about 40x the Haiku price)."""
    from decimal import Decimal

    from pocketpaw_ee.site_ai import aliases

    usage = {
        "input_tokens": 2,
        "output_tokens": 22,
        "cached_input_tokens": 752,
        "cache_read_tokens": 0,
        "cache_write_tokens": 752,
        "total_cost_usd": 0.0065918,
        "model": "claude-haiku-5-5",
    }
    cost = aliases.cost_usd("fast", None, usage, (754, 22))
    # 2 x $0.10 + 752 x $0.125 (a write is 1.25x input) + 22 x $0.50, per MTok
    expected = (
        Decimal(2) * Decimal("0.10")
        + Decimal(752) * Decimal("0.125")
        + Decimal(22) * Decimal("0.50")
    ) / Decimal(1_000_000)
    assert cost == expected


def test_haiku_5_5_bills_its_long_context_tier_past_100k():
    from decimal import Decimal

    from pocketpaw_ee.site_ai import aliases

    m = Decimal(1_000_000)
    short = aliases.price_usd("claude-haiku-5-5", input_tokens=100_000, output_tokens=1000)
    long = aliases.price_usd("claude-haiku-5-5", input_tokens=100_001, output_tokens=1000)
    assert short == (Decimal(100_000) * Decimal("0.10") + Decimal(1000) * Decimal("0.50")) / m
    assert long == (Decimal(100_001) * Decimal("0.50") + Decimal(1000) * Decimal("2.50")) / m


def test_an_unknown_model_has_no_price():
    from pocketpaw_ee.site_ai import aliases

    for model in ("mystery-model", "claude", "", None):
        assert aliases.price_usd(model, input_tokens=10, output_tokens=10) is None
    usage = {"input_tokens": 10, "output_tokens": 10, "model": "mystery-model"}
    with pytest.raises(aliases.Unpriced):
        aliases.cost_usd("fast", None, usage, (10, 10))


async def test_an_unpriced_model_is_refused_before_the_call(beanie_test_db, monkeypatch, caplog):
    """A model with no verified price never runs: nothing could bill it honestly."""
    from pocketpaw_ee.cloud.chat import side_completion
    from pocketpaw_ee.cloud.chat.side_completion import SideTarget

    site, token = await _setup()
    fake = FakeLlm(monkeypatch)

    async def _target(workspace_id, user_id, agent_id):
        return SideTarget(agent_id="agent-x", payer="platform", model="mystery-model")

    monkeypatch.setattr(side_completion, "resolve_target", _target)
    caplog.set_level(logging.ERROR, logger="pocketpaw_ee.site_ai.chat")
    async with client() as c:
        resp = await _post(c, site, token, _body(stream=False))

    assert resp.status_code == 503
    assert fake.calls == []
    assert any("no verified price" in r.getMessage() for r in caplog.records)


async def test_the_call_log_names_requested_and_reported_models(
    beanie_test_db, monkeypatch, caplog
):
    """The CLI's own cost is kept only as a comparison field, and a backend that
    answers on another model than the one pinned is a WARNING."""
    site, token = await _setup()
    usage = {
        "input_tokens": 2,
        "output_tokens": 22,
        "cache_write_tokens": 752,
        "cache_read_tokens": 0,
        "total_cost_usd": 0.0065918,
        "model": "claude-haiku-5-5",
    }
    FakeLlm(monkeypatch, script=["Hi", ok("Hi", usage=usage)])
    caplog.set_level(logging.INFO, logger="pocketpaw_ee.site_ai.chat")
    async with client() as c:
        await _post(c, site, token, _body())

    line = next(r.getMessage() for r in caplog.records if "site_ai.call" in r.getMessage())
    assert "model_requested=claude-sonnet-5-5" in line
    assert "model_reported=claude-haiku-5-5" in line
    assert "cli_cost_usd=0.0065918" in line
    assert any(
        r.levelno == logging.WARNING and "model mismatch" in r.getMessage() for r in caplog.records
    )


def test_site_calls_pin_their_model():
    """Every site AI side call asks the side completion for an explicit model."""
    from types import SimpleNamespace

    from pocketpaw_ee.site_ai import llm

    call = SimpleNamespace(
        messages=[{"role": "user", "content": "hi"}],
        response_format=None,
        max_tokens=64,
        workspace_id="ws",
        owner_id="u",
        agent_id=None,
        model=None,
    )
    kw = llm._kwargs(call)
    assert kw["pin_model"] is True and kw["isolated"] is True


# ------------------------------------------------------------------ latency


async def test_done_is_sent_before_the_billing_writes(beanie_test_db, monkeypatch):
    """The visitor's stream ends (``done``) before the run doc, the settle and the
    alerts are written; the writes still happen.

    FAILS before the fix: ``record`` awaited every write before ``done``."""
    from pocketpaw_ee.site_ai import budget, chat

    site, token = await _setup()
    FakeLlm(monkeypatch, script=["Hello ", "there", ok("Hello there")])
    order: list[str] = []
    real_sse, real_write, real_settle = chat.sse, chat._write_run, budget.settle

    def _sse(event, data):
        order.append(event)
        return real_sse(event, data)

    async def _write(*a, **kw):
        order.append("write_run")
        return await real_write(*a, **kw)

    async def _settle(*a, **kw):
        order.append("settle")
        return await real_settle(*a, **kw)

    monkeypatch.setattr(chat, "sse", _sse)
    monkeypatch.setattr(chat, "_write_run", _write)
    monkeypatch.setattr(budget, "settle", _settle)
    async with client() as c:
        resp = await _post(c, site, token, _body())

    assert [e for e, _ in parse_sse(resp.text)] == ["chunk", "chunk", "usage", "done"]
    assert order.index("done") < order.index("write_run")
    assert order.index("done") < order.index("settle")


async def test_chunks_are_forwarded_as_the_backend_yields_them(beanie_test_db, monkeypatch):
    """Each delta becomes its own ``chunk`` frame; nothing is joined or held."""
    site, token = await _setup()
    FakeLlm(monkeypatch, script=["One ", "two ", "three", ok("One two three")])
    async with client() as c:
        resp = await _post(c, site, token, _body())
    chunks = [d["delta"] for e, d in parse_sse(resp.text) if e == "chunk"]
    assert chunks == ["One ", "two ", "three"]


async def test_the_call_log_carries_ttft_and_total(beanie_test_db, monkeypatch, caplog):
    site, token = await _setup()
    FakeLlm(monkeypatch, script=["Hi", ok("Hi")])
    caplog.set_level(logging.INFO, logger="pocketpaw_ee.site_ai.chat")
    async with client() as c:
        await _post(c, site, token, _body())
    line = next(r.getMessage() for r in caplog.records if "site_ai.call" in r.getMessage())
    assert "ttft_ms=" in line and "total_ms=" in line and "hold_ratio=" in line


# ------------------------------------------------------------------ cache prefix


async def test_the_system_prompt_is_byte_identical_across_calls(beanie_test_db, monkeypatch):
    """Two visitors of one site, with different questions and different output
    caps, send the same system prompt, so the backend's cached prefix (and its
    standby client) can be reused.

    FAILS before the fix: the per-request output cap was appended to the system
    prompt, so a different ``max_output_tokens`` changed it."""
    site, token = await _setup()
    fake = FakeLlm(monkeypatch, script=["ok", ok("ok")])
    async with client() as c:
        await _post(c, site, token, _body("What time do you open?", max_output_tokens=100))
        await _post(c, site, token, _body("Do you deliver?", max_output_tokens=300))

    first, second = fake.calls
    assert first["system"].encode() == second["system"].encode()
    assert first["system"].startswith(SYSTEM)
    assert "What time do you open?" not in second["prompt"]
    assert "about 300 tokens" in second["prompt"]
