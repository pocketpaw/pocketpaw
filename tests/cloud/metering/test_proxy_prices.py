# tests/cloud/metering/test_proxy_prices.py — pricing a run from OUR proxy's rates.
#
# THE GAP THIS CLOSES. The pricing ladder asked genai-prices and then a hand table,
# both of them public lists of published rates. A model served through our LiteLLM
# proxy at a negotiated rate billed at list, and a model that exists ONLY on our
# proxy — a fine-tune, an alias, a self-hosted weight — appears in no public list,
# so it priced as None and the run billed zero. The second one is the expensive
# case: it is a bill we never send, for exactly the models we chose to run.
#
# WHAT MATTERS HERE. That a custom model prices at all; that the proxy wins over
# the public list where they disagree, because the proxy is the one that is right
# about what WE pay; and that every failure is a miss rather than an error, since
# this runs on the billing path and a run must not die over its own invoice.
#
# Created 2026-09-04 (fix/proxy-model-prices): new test module.

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pocketpaw_ee.cloud.metering import proxy_prices

_AT = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)

# A custom entry: no public price list carries this id.
_CUSTOM_ROW = {
    "model_name": "paw/house-sonnet",
    "litellm_params": {"model": "openai/ft:house-sonnet-2026"},
    "model_info": {
        "input_cost_per_token": 0.000002,
        "output_cost_per_token": 0.00001,
        "cache_read_input_token_cost": 0.0000002,
        "cache_creation_input_token_cost": 0.0000025,
    },
}

# A standard model we happen to serve at a rate we negotiated.
_NEGOTIATED_ROW = {
    "model_name": "claude-sonnet-5",
    "litellm_params": {"model": "anthropic/claude-sonnet-5"},
    "model_info": {"input_cost_per_token": 0.0000015, "output_cost_per_token": 0.0000075},
}


@pytest.fixture(autouse=True)
def _clean_snapshot():
    """Every test starts with no provider and no snapshot, and leaves none behind.

    The snapshot is module state on the billing path, so a test that leaked one
    would reprice unrelated suites.
    """
    proxy_prices.unregister()
    yield
    proxy_prices.unregister()


def _load(rows) -> None:
    """Install a snapshot directly, without a proxy."""
    proxy_prices._PRICES.update(proxy_prices._snapshot_from_rows(rows))


def test_a_model_only_our_proxy_serves_gets_priced():
    """The zero-bill case. Nothing public carries this id, so before this rung it
    priced as None and the run was billed nothing at all."""
    _load([_CUSTOM_ROW])

    cost = proxy_prices.price_from_proxy(
        "paw/house-sonnet",
        input_tokens=1000,
        output_tokens=500,
        cache_read_tokens=0,
        cache_write_tokens=0,
        at=_AT,
    )

    # 1000 * 0.000002 + 500 * 0.00001
    assert cost == Decimal("0.002") + Decimal("0.005")


def test_the_upstream_id_prices_the_same_as_the_alias():
    """A run's reported model is not normalised. Our own requests name the proxy
    alias; a backend going through pydantic-ai reports the upstream id. Both have
    to reach the same price or half the runs fall through to the public list."""
    _load([_CUSTOM_ROW])

    args = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "at": _AT,
    }
    assert proxy_prices.price_from_proxy("paw/house-sonnet", **args) == (
        proxy_prices.price_from_proxy("openai/ft:house-sonnet-2026", **args)
    )


def test_cache_tokens_are_priced_at_their_own_rates_and_not_charged_twice():
    """``input_tokens`` is the INCLUSIVE prompt total, so the cache buckets are
    subsets of it. Charging them at the input rate as well would bill those tokens
    twice, and the hand table has no cache-write column at all."""
    _load([_CUSTOM_ROW])

    cost = proxy_prices.price_from_proxy(
        "paw/house-sonnet",
        input_tokens=1000,
        output_tokens=0,
        cache_read_tokens=600,
        cache_write_tokens=200,
        at=_AT,
    )

    # 200 plain * 2e-6 + 600 read * 2e-7 + 200 write * 2.5e-6
    expected = Decimal("0.0004") + Decimal("0.00012") + Decimal("0.0005")
    assert cost == expected


def test_a_model_without_cache_rates_falls_back_to_the_input_rate():
    """What LiteLLM itself does for a model with no cache pricing. Dropping the
    cache tokens instead would serve them free."""
    _load([_NEGOTIATED_ROW])

    cost = proxy_prices.price_from_proxy(
        "claude-sonnet-5",
        input_tokens=1000,
        output_tokens=0,
        cache_read_tokens=400,
        cache_write_tokens=0,
        at=_AT,
    )

    assert cost == Decimal("1000") * Decimal("0.0000015")


def test_a_model_the_proxy_does_not_serve_is_a_miss():
    """None means "not my model" and falls through to the public lists. It must
    never be Decimal(0), which would bill the run at nothing."""
    _load([_CUSTOM_ROW])

    assert (
        proxy_prices.price_from_proxy(
            "gpt-5",
            input_tokens=1000,
            output_tokens=100,
            cache_read_tokens=0,
            cache_write_tokens=0,
            at=_AT,
        )
        is None
    )


def test_rates_are_decimal_all_the_way_through():
    """These are numbers like 1.5e-06 multiplied by token counts in the hundreds of
    thousands. In binary floating point the ledger's exact-sum invariant stops
    holding, which is the reason credits are integers in the first place."""
    _load([_NEGOTIATED_ROW])

    cost = proxy_prices.price_from_proxy(
        "claude-sonnet-5",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        at=_AT,
    )

    assert isinstance(cost, Decimal)
    assert cost == Decimal("1.5") + Decimal("7.5")


def test_one_upstream_aliased_at_two_prices_is_not_priced_here():
    """Aliasing one upstream model twice at different markups is an ordinary LiteLLM
    config, and a run reporting the upstream id does not say which alias served it.
    Registering the first row's price would bill it at whichever the proxy happened
    to list first — arbitrary, silent, and about money. Better to fall through."""
    _load(
        [
            {
                "model_name": "house/cheap",
                "litellm_params": {"model": "anthropic/claude-sonnet-5"},
                "model_info": {"input_cost_per_token": 0.000001},
            },
            {
                "model_name": "house/premium",
                "litellm_params": {"model": "anthropic/claude-sonnet-5"},
                "model_info": {"input_cost_per_token": 0.000009},
            },
        ]
    )

    # Both aliases price, because the proxy makes those unique.
    assert "house/cheap" in proxy_prices._PRICES
    assert "house/premium" in proxy_prices._PRICES
    # The shared upstream does not, because we cannot tell which one a run used.
    assert "anthropic/claude-sonnet-5" not in proxy_prices._PRICES


def test_one_upstream_aliased_twice_at_the_same_price_still_works():
    """The ambiguity is about disagreement, not about duplication. Two aliases at
    the same rate leave the upstream id perfectly well defined."""
    row = {
        "litellm_params": {"model": "anthropic/claude-sonnet-5"},
        "model_info": {"input_cost_per_token": 0.000001},
    }
    _load([{**row, "model_name": "house/a"}, {**row, "model_name": "house/b"}])

    assert "anthropic/claude-sonnet-5" in proxy_prices._PRICES


def test_an_alias_beats_an_upstream_id_that_spells_the_same_thing():
    """When one row's alias equals another row's upstream string, the alias is the
    proxy's own name for a served model and wins."""
    _load(
        [
            {
                "model_name": "anthropic/claude-sonnet-5",
                "model_info": {"input_cost_per_token": 0.000002},
            },
            {
                "model_name": "house/rebadged",
                "litellm_params": {"model": "anthropic/claude-sonnet-5"},
                "model_info": {"input_cost_per_token": 0.000009},
            },
        ]
    )

    assert proxy_prices._PRICES["anthropic/claude-sonnet-5"].input == Decimal("0.000002")


def test_a_row_with_no_usable_rate_is_skipped():
    """A malformed or unpriced row must not register an id that then prices at
    zero — falling through to the public list is the better answer."""
    _load(
        [
            {"model_name": "broken", "model_info": {"input_cost_per_token": "not-a-number"}},
            {"model_name": "negative", "model_info": {"input_cost_per_token": -1}},
            {"model_name": "no-info"},
        ]
    )

    assert "broken" not in proxy_prices._PRICES
    assert "negative" not in proxy_prices._PRICES
    assert "no-info" not in proxy_prices._PRICES


# ---------------------------------------------------------------------------
# The ladder: this rung sits above the public lists, and below it nothing changes.
# ---------------------------------------------------------------------------


def test_the_proxy_price_wins_over_the_public_one():
    """The captain's ask, stated as a test. ``claude-sonnet-5`` is $3/$15 publicly;
    we serve it at $1.50/$7.50. Filling only the gaps would leave this billed at
    list, so the rung goes above genai-prices rather than under it."""
    from pocketpaw.usage_tracker import price_run

    _load([_NEGOTIATED_ROW])
    proxy_prices.register()

    cost = price_run(
        "claude-sonnet-5",
        input_tokens=1_000_000,
        output_tokens=0,
        at=_AT,
    )

    assert cost == Decimal("1.5")


def test_without_a_provider_the_public_ladder_is_untouched():
    """OSS, and any deployment that registers nothing, must price exactly as it
    did. The provider is opt-in."""
    from pocketpaw.usage_tracker import price_run

    public = price_run("claude-sonnet-5", input_tokens=1_000_000, output_tokens=0, at=_AT)

    assert public is not None
    assert public != Decimal("1.5")


def test_a_provider_that_raises_is_a_miss_not_an_outage():
    """This runs while billing a run. Nothing it does may reach the caller."""
    from pocketpaw.usage_tracker import price_run, set_price_provider

    def _explode(*_args, **_kwargs):
        raise RuntimeError("proxy on fire")

    set_price_provider(_explode)
    try:
        cost = price_run("claude-sonnet-5", input_tokens=1_000_000, output_tokens=0, at=_AT)
    finally:
        set_price_provider(None)

    # Fell through to the public list rather than raising.
    assert cost is not None


async def test_a_failed_refresh_keeps_the_previous_snapshot(monkeypatch):
    """A proxy blip must not silently empty the price list. Dropping it would send
    every custom model back to billing zero, which is the failure this rung exists
    to fix — and it would do it quietly."""
    _load([_CUSTOM_ROW])
    before = dict(proxy_prices._PRICES)

    class _Broken:
        async def model_info(self):
            raise RuntimeError("proxy unreachable")

    monkeypatch.setattr(
        "pocketpaw_ee.catalog.litellm_client.LiteLLMClient", lambda *a, **k: _Broken()
    )

    held = await proxy_prices.refresh(force=True)

    assert held == len(before)
    assert proxy_prices._PRICES.keys() == before.keys()


async def test_refresh_installs_what_the_proxy_reports(monkeypatch):
    class _Proxy:
        async def model_info(self):
            return [_CUSTOM_ROW, _NEGOTIATED_ROW]

    monkeypatch.setattr(
        "pocketpaw_ee.catalog.litellm_client.LiteLLMClient", lambda *a, **k: _Proxy()
    )

    count = await proxy_prices.refresh(force=True)

    assert count == 4  # two rows, alias + upstream id each
    assert "paw/house-sonnet" in proxy_prices._PRICES
    assert "anthropic/claude-sonnet-5" in proxy_prices._PRICES
