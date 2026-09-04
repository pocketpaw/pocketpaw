# ee/pocketpaw_ee/cloud/metering/proxy_prices.py — price a run from OUR LiteLLM
# proxy's configured rates rather than a public price list.
#
# WHY. ``usage_tracker.price_run`` asks genai-prices, then a hand table. Both are
# somebody else's list of published rates, and neither knows what we pay. Two
# things go wrong because of that. A model we serve at a negotiated rate bills at
# list. And a model that exists only on our proxy — a fine-tune, a deployment
# alias, a self-hosted weight — is in no public list at all, so it prices as None
# and the run bills ZERO. That is not a rounding error; it is a bill we never send,
# for exactly the models we chose to run ourselves.
#
# The proxy already holds the right number. ``GET /model/info`` returns
# ``input_cost_per_token`` / ``output_cost_per_token`` per model, and for a custom
# entry those are whatever we configured. This module keeps a snapshot of that and
# registers it as ``usage_tracker``'s top price rung, so every consumer of the
# ladder — the metering sweeper, the runtime tracker — gets our rates.
#
# WHY A SNAPSHOT AND NOT A LOOKUP. ``price_run`` is synchronous and is called from
# ``metering.service.resolve_cost``, which is synchronous too, on a sweeper that
# bills up to 200 runs a tick. An HTTP call per run is out of the question, and
# there is no event loop to await on at that point anyway. So the fetch is async
# and separate: ``refresh()`` runs at cloud boot and again at the top of each
# metering sweep, and the provider itself is a dict lookup and some arithmetic.
#
# WHAT THIS COSTS, said plainly. A proxy price is CURRENT; it carries no effective
# date. The rungs below price a run at the rate in force when it RAN, which is why
# they exist — the sweeper drains a backlog that can span days, and repricing it at
# today's rate is the bug that work fixed on 2026-09-02. A run priced here gets
# today's rate. We take that trade because a confidently wrong public price, or no
# price at all, is worse than a current one. If a proxy rate changes mid-backlog,
# the affected runs bill at the new rate.
#
# FAIL OPEN, always. An unreachable proxy, a malformed row, a model the proxy does
# not serve: all of them are a miss, and a miss falls through to genai-prices
# exactly as before. Nothing here can make a run fail or a bill raise.
#
# Created 2026-09-04 (fix/proxy-model-prices): new module.

from __future__ import annotations

import logging
import time
from datetime import datetime
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

#: How long a fetched snapshot is considered current, in seconds. The metering
#: sweep runs on a five minute heartbeat and refreshes at the top of each tick, so
#: this mostly guards against a second caller refetching within the same tick.
_TTL_SECONDS = 300

#: Per-request timeout for the ``/model/info`` read. Deliberately shorter than the
#: catalog client's own default, because this runs on the boot path.
_FETCH_TIMEOUT_SECONDS = 5.0

#: model id -> per-token USD costs. Empty until ``refresh`` succeeds once, which is
#: the OSS / proxy-less state and prices nothing.
_PRICES: dict[str, _ModelPrice] = {}
_FETCHED_AT: float = 0.0

#: Failed fetches since the last good one. Keeps ``_log_fetch_failure`` loud once
#: and quiet after, since the sweep retries every five minutes.
_CONSECUTIVE_FAILURES: int = 0


class _ModelPrice:
    """Per-token USD costs for one model, as the proxy reports them.

    Stored as ``Decimal`` from the start. These are numbers like 1.5e-06 and they
    get multiplied by token counts in the hundreds of thousands; doing that in
    binary floating point is how a ledger that promises exact sums stops being
    exact.
    """

    __slots__ = ("cache_read", "cache_write", "input", "output")

    def __init__(
        self,
        *,
        input_: Decimal | None,
        output: Decimal | None,
        cache_read: Decimal | None,
        cache_write: Decimal | None,
    ) -> None:
        self.input = input_
        self.output = output
        self.cache_read = cache_read
        self.cache_write = cache_write

    def prices_anything(self) -> bool:
        return self.input is not None or self.output is not None

    def __eq__(self, other: object) -> bool:
        """Value equality. ``_snapshot_from_rows`` compares two aliases' prices to
        decide whether their shared upstream id is safe to register, and identity
        would call every pair different."""
        if not isinstance(other, _ModelPrice):
            return NotImplemented
        return (self.input, self.output, self.cache_read, self.cache_write) == (
            other.input,
            other.output,
            other.cache_read,
            other.cache_write,
        )

    def __hash__(self) -> int:
        return hash((self.input, self.output, self.cache_read, self.cache_write))


def _decimal(value: Any) -> Decimal | None:
    """A per-token cost as a Decimal, or None for missing / junk / negative.

    ``Decimal(str(x))`` rather than ``Decimal(x)``: the proxy hands these over JSON
    as floats, and converting the float directly carries its binary error into the
    ledger.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = Decimal(str(value))
    except Exception:  # noqa: BLE001 — a malformed rate is a miss, not an error
        return None
    if not dec.is_finite() or dec < 0:
        return None
    return dec


def _alias_of(row: dict[str, Any]) -> str:
    """The name the proxy serves this row under. Unique across the proxy config,
    and what our own requests name."""
    return (row.get("model_name") or "").strip()


def _upstream_of(row: dict[str, Any]) -> str:
    """The provider id underneath the alias, which is what a backend going through
    pydantic-ai or ChatLiteLLM tends to report back in its usage. NOT unique: one
    upstream model is routinely aliased several times, at different markups."""
    params = row.get("litellm_params")
    if not isinstance(params, dict):
        return ""
    return (params.get("model") or "").strip()


def _price_of(row: dict[str, Any]) -> _ModelPrice | None:
    """Per-token costs from one ``/model/info`` row, or None when it prices nothing."""
    info = row.get("model_info")
    if not isinstance(info, dict):
        return None
    price = _ModelPrice(
        input_=_decimal(info.get("input_cost_per_token")),
        output=_decimal(info.get("output_cost_per_token")),
        # LiteLLM names the cache rates separately, and the hand table has never had
        # a cache-WRITE column at all — a write-heavy turn that falls through to it
        # undercounts. Carry both when the proxy reports them.
        cache_read=_decimal(info.get("cache_read_input_token_cost")),
        cache_write=_decimal(info.get("cache_creation_input_token_cost")),
    )
    return price if price.prices_anything() else None


def _snapshot_from_rows(rows: list[dict[str, Any]]) -> dict[str, _ModelPrice]:
    """Map ``/model/info`` rows onto the price table. Never raises.

    Aliases are registered unconditionally: the proxy makes them unique, so each one
    has exactly one price.

    Upstream ids are registered ONLY when every alias sharing them agrees on the
    price. Aliasing one upstream model several times at different markups is an
    ordinary LiteLLM config, and a run that reports the upstream id gives us no way
    to tell which alias served it. Taking the first row would bill it at whichever
    alias the proxy happened to list first — a silent, arbitrary choice on a money
    path. Leaving the ambiguous id out sends those runs to the public price list
    instead, which is at least a defensible number, and says so in the log.
    """
    table: dict[str, _ModelPrice] = {}
    upstream_seen: dict[str, _ModelPrice | None] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        price = _price_of(row)
        if price is None:
            continue

        alias = _alias_of(row)
        if alias:
            table[alias] = price

        upstream = _upstream_of(row)
        if not upstream or upstream == alias:
            continue
        if upstream not in upstream_seen:
            upstream_seen[upstream] = price
        elif upstream_seen[upstream] != price:
            # Two aliases, one upstream, different money. Mark it unusable.
            upstream_seen[upstream] = None

    for upstream, price in upstream_seen.items():
        if upstream in table:
            # An alias already claims this exact string; the alias is authoritative.
            continue
        if price is None:
            logger.info(
                "metering.proxy_prices: upstream id %r is aliased at more than one "
                "price — not pricing it here, since a run reporting it does not say "
                "which alias served it. Those runs fall through to the public list.",
                upstream,
            )
            continue
        table[upstream] = price

    return table


async def refresh(*, force: bool = False) -> int:
    """Re-read ``/model/info`` into the snapshot. Returns how many ids it holds.

    A no-op inside the TTL unless ``force``. Any failure leaves the PREVIOUS
    snapshot in place and returns its size — a proxy blip must not silently drop
    every custom price and start billing those runs at zero again.
    """
    global _FETCHED_AT, _CONSECUTIVE_FAILURES

    if not force and _PRICES and (time.monotonic() - _FETCHED_AT) < _TTL_SECONDS:
        return len(_PRICES)

    try:
        from pocketpaw_ee.catalog.litellm_client import LiteLLMClient

        # Five seconds, not the client's default fifteen. This runs on the cloud
        # boot path, and an unreachable proxy would otherwise stall every start by
        # a quarter of a minute for something the deployment can live without.
        rows = await LiteLLMClient(timeout=_FETCH_TIMEOUT_SECONDS).model_info()
        table = _snapshot_from_rows(rows)
    except Exception:  # noqa: BLE001 — the proxy is optional and may be down
        _log_fetch_failure(
            "could not read /model/info (an unset POCKETPAW_LITELLM_API_KEY against "
            "a key-protected proxy looks exactly like this)"
        )
        return len(_PRICES)

    if not table:
        # An empty proxy is a real answer, but so is a proxy that answered with
        # nothing useful. Neither is worth discarding a good snapshot for.
        _log_fetch_failure("/model/info returned no priced model")
        return len(_PRICES)

    if _CONSECUTIVE_FAILURES:
        logger.info(
            "metering.proxy_prices: /model/info readable again after %d failed attempt(s)",
            _CONSECUTIVE_FAILURES,
        )
    _CONSECUTIVE_FAILURES = 0
    _PRICES.clear()
    _PRICES.update(table)
    _FETCHED_AT = time.monotonic()
    logger.info("metering.proxy_prices: %d model id(s) priced from the proxy", len(_PRICES))
    return len(_PRICES)


def _log_fetch_failure(what: str) -> None:
    """Loud the first time, quiet after that until it recovers.

    This is retried at the top of every metering sweep, so a proxy that is simply
    not configured — or one rejecting an unset API key — would otherwise emit the
    same warning with the same stack trace every five minutes forever. The first
    one is the useful one; the recovery is logged too, so the pair brackets the
    outage instead of filling the log with it.
    """
    global _CONSECUTIVE_FAILURES

    _CONSECUTIVE_FAILURES += 1
    message = (
        "metering.proxy_prices: %s — keeping the previous snapshot of %d model(s); "
        "runs price from the public lists until this recovers"
    )
    if _CONSECUTIVE_FAILURES == 1:
        logger.warning(message, what, len(_PRICES), exc_info=True)
    else:
        logger.debug(message, what, len(_PRICES))


def price_from_proxy(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    at: datetime,
) -> Decimal | None:
    """The registered price provider. Synchronous, pure, and never raises.

    ``input_tokens`` is the INCLUSIVE prompt total, matching ``price_run``'s
    contract, so the cache buckets are subtracted out of it here rather than
    charged twice. A cache rate the proxy does not report falls back to the input
    rate, which is what LiteLLM itself does for a model with no cache pricing.

    ``at`` is accepted and ignored: a proxy rate carries no effective date. It is
    in the signature because the provider protocol has it and because a future
    dated source would need it.
    """
    price = _PRICES.get(model)
    if price is None:
        return None

    in_rate = price.input
    out_rate = price.output
    if in_rate is None and out_rate is None:
        return None

    read = max(0, cache_read_tokens)
    write = max(0, cache_write_tokens)
    plain = max(0, input_tokens - read - write)

    total = Decimal(0)
    if in_rate is not None:
        total += in_rate * plain
        total += (price.cache_read if price.cache_read is not None else in_rate) * read
        total += (price.cache_write if price.cache_write is not None else in_rate) * write
    if out_rate is not None:
        total += out_rate * max(0, output_tokens)
    return total


def register() -> None:
    """Install ``price_from_proxy`` as ``usage_tracker``'s top price rung."""
    from pocketpaw.usage_tracker import set_price_provider

    set_price_provider(price_from_proxy)


def unregister() -> None:
    """Remove the provider and drop the snapshot. For tests and for a deployment
    that wants the public lists back without a restart."""
    from pocketpaw.usage_tracker import set_price_provider

    global _FETCHED_AT, _CONSECUTIVE_FAILURES
    set_price_provider(None)
    _PRICES.clear()
    _FETCHED_AT = 0.0
    _CONSECUTIVE_FAILURES = 0
