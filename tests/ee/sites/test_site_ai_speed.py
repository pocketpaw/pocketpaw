# tests/ee/sites/test_site_ai_speed.py: site AI latency, prompt-cache stability
# and the size of the billing hold.
#
# Production (2026-11-05, Haiku 5.5 on the Claude SDK backend): almost every call
# wrote its whole prompt to the cache (cache_write ~750-1250 tokens) and the
# backend's reported cost ran up to twice the hold ("cost 1638700 over its 813751
# hold; billing the hold"), so the platform absorbed the difference. These tests
# pin: a hold that covers a prompt written to cache, a hold that learns a model's
# real rate after one overrun, ``done`` sent before the billing writes, and a
# system prompt that stays byte-identical per site across calls.
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
    monkeypatch.setattr(aliases, "_RATE_SCALE", {}, raising=False)
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
    for model in ("claude-haiku-4-5", "claude-sonnet-4-5", None):
        hold = aliases.reserve_micro("fast", model, estimate, cap)
        cost = aliases.to_micro(aliases.cost_usd("fast", model, usage, (estimate, cap)))
        assert cost <= hold, f"{model}: cost {cost} over hold {hold}"


async def test_a_reported_cost_over_the_hold_is_logged_and_the_next_hold_covers_it(
    beanie_test_db, monkeypatch, caplog
):
    """The backend's reported cost can price a model above anything we hold for
    it (production: 1638700 against an 813751 hold). The overrun is clamped and
    logged at WARNING with its ratio, and the next hold for that model is sized
    from what it really cost, so it does not overrun again.

    FAILS before the fix: every call held the same and overran the same."""
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc

    site, token = await _setup()
    usage = {
        "input_tokens": 2,
        "output_tokens": 19,
        "cached_input_tokens": 40,
        "cache_read_tokens": 0,
        "cache_write_tokens": 40,
        # Far above any rate we hold at, as the CLI's own price list can be.
        "total_cost_usd": 0.01,
        "model": "claude-haiku-5-5",
    }
    FakeLlm(monkeypatch, script=["Hi!", ok("Hi!", usage=usage)])
    caplog.set_level(logging.WARNING, logger="pocketpaw_ee.site_ai.chat")
    async with client() as c:
        await _post(c, site, token, _body(max_output_tokens=64))
        await _post(c, site, token, _body(max_output_tokens=64))

    rows = await SiteAiCallDoc.find_all().sort("+created_at").to_list()
    assert len(rows) == 2
    first, second = rows
    assert first.actual_micro == first.reserved_micro  # clamped at its hold
    over = [r for r in caplog.records if "over its" in r.getMessage()]
    assert over and "ratio=" in over[0].getMessage()
    assert second.reserved_micro >= second.actual_micro
    assert len(over) == 1, "the second call must not overrun its hold again"


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
