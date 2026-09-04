# Usage tracker — persistent token/cost tracking across sessions.
# Created: 2026-03-09
# Updated: 2026-09-04 (fix/proxy-model-prices) — ``set_price_provider`` adds a
#   rung ABOVE genai-prices for the deployment's own price list. Every rung
#   below is somebody else's published rates and none of them know what WE
#   pay: a model served at a negotiated rate billed at list, and a model that
#   exists only on our proxy is in no public list at all, so it priced None and
#   billed ZERO. The cloud registers the LiteLLM proxy's configured costs; OSS
#   registers nothing and prices exactly as before. The trade is dating — a
#   proxy rate is current, not effective-dated. See set_price_provider.
# Updated: 2026-09-02 (fix/metering-dated-pricing) — pricing is no longer a flat
#   hand table. ``price_run`` is the front door: it asks ``genai-prices`` for an
#   EFFECTIVE-DATED rate at the run's own moment, falls back to the same lookup
#   with a provider prefix stripped, and only then reads ``_PRICING`` below. It
#   returns ``Decimal | None`` and never raises; ``None`` means "could not
#   price" and is not the same fact as ``Decimal("0")``. ``_estimate_cost`` is
#   unchanged in signature and is now rung 3 of that ladder. See the long note
#   above ``_PRICING`` for why the table stayed and which rows were wrong.
#
# Stores per-request usage records as append-only JSONL in ~/.pocketpaw/usage.jsonl.
# Provides aggregation helpers for the /api/v1/metrics/usage endpoint.
#
# Budget enforcement:
#   record() performs a cumulative-spend check against the configured cap
#   before writing to disk.  When budget_auto_pause is True and the cap is
#   exhausted, it raises BudgetExhaustedError.  The AgentLoop preflight
#   catches this before routing to the LLM, but the check here ensures
#   enforcement even in code paths that call record() directly.

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class PriceProvider(Protocol):
    """A deployment's own price list. See ``set_price_provider``.

    Returns the run's USD cost, or ``None`` for a model it does not price (which
    falls through to the public price rungs). Must not raise.
    """

    def __call__(
        self,
        model: str,
        *,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        at: datetime,
    ) -> Decimal | None: ...


# ── Pricing" banner through the end of _estimate_cost.
# Banked 2026-09-02 for fix/metering-dated-pricing.
# ── Pricing ───────────────────────────────────────────────────────────────────
# Rewritten 2026-09-02 (fix/metering-dated-pricing).
#
# THE TABLE BELOW WAS RIGHT WHEN IT WAS WRITTEN AND WENT WRONG WITHOUT ANYONE
# TOUCHING IT. Two rows measured wrong on 2026-09-01: ``gemini-2.5-flash`` read
# $0.15/$0.60 here against $0.30/$2.50 in reality, and ``claude-sonnet-5`` read
# $2.00/$10.00 against $3.00/$15.00. Nobody edited either line. A hand table has
# no way to learn that a price moved — and no way to express that a price has a
# DATE, which is the deeper problem. Billing runs on a sweeper that drains 200
# runs a tick, so a backlog spans hours or days; a table with one flat number per
# model prices Sunday's run at Tuesday's rate and looks entirely correct doing it.
#
# Pricing is therefore a LADDER now, and this table is its last rung:
#
#   1. ``genai-prices`` — a maintained, EFFECTIVE-DATED price set compiled into
#      the package as Python source (``genai_prices/data.py``). It is already an
#      unconditional requirement of ``pydantic-ai-slim``, which ``ee`` depends on
#      hard, so it is present everywhere metering runs; it is now an explicit
#      dependency rather than a borrowed transitive one. It prices cache reads and
#      cache WRITES separately (a write is 1.25x input, which this table has never
#      had a column for), applies Anthropic's >200k long-context tier on its own,
#      and takes the RUN'S OWN timestamp, so a backlogged run bills at the rate
#      that was in force when it ran. Offline by construction: the price updater
#      is opt-in and nothing on this path starts it.
#   2. The same lookup with a ``provider/`` prefix stripped. Model strings are not
#      normalised upstream and no single form can be assumed — ``deepseek-v3.2``
#      (a model-group alias), ``deepseek-chat`` (the upstream bare name) and
#      ``deepseek/deepseek-chat`` (provider-prefixed) are all reachable, and the
#      library rejects the third.
#   3. ``_PRICING`` below. It stays because the library genuinely does not carry
#      every id we run. Measured against genai-prices 0.0.73 on 2026-09-02:
#      ``claude-haiku-4-20250506``, ``codex-mini-latest``, the bare ``"claude"``
#      the agentapi path reports and ``"(codex-config)"`` all miss. Deleting the
#      table would silently unprice those four.
#
# ``price_run`` returns ``None`` for "could not price". ``None`` is NOT
# ``Decimal("0")`` and a caller must never collapse the two — a $0 bill and an
# unknown bill are different facts, and treating the second as the first is
# exactly how it went unnoticed for two days that this table had gone stale.
# It never raises: a run must not die over its own invoice.
#
# USD per MILLION tokens. ``cached_input`` is the cache-HIT (read) rate. Rung 3
# still has no cache-WRITE column, so a write-heavy turn that falls all the way
# through undercounts; rungs 1-2 price the write correctly.
#
# **When a model ships, prefer letting rung 1 price it.** Add a row here only for
# an id the library does not carry. The table lookup now takes the LONGEST match
# in either direction instead of the first hit in insertion order — the old scan
# matched ``gpt-4.1-mini-2025-04-14`` against ``gpt-4.1`` and overcharged it 5x.
# Insertion order therefore no longer changes any price, which retires the reason
# the current families had to be appended rather than sorted. The bare
# ``"claude"`` the agentapi path uses now takes the longest claude key,
# ``claude-3-5-sonnet-20241022`` — the same $3.00/$15.00/$0.30 it has always
# resolved to via ``claude-sonnet-4-20250514``, so nothing repriced.
#
# Rates from https://platform.claude.com/docs/en/about-claude/pricing, read
# 2026-08-21; the two stale rows re-measured against genai-prices 0.0.73 on
# 2026-09-02.
_PRICING: dict[str, dict[str, float]] = {
    # Anthropic
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0, "cached_input": 0.30},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0, "cached_input": 1.50},
    "claude-haiku-4-20250506": {"input": 0.80, "output": 4.0, "cached_input": 0.08},
    "claude-3-5-sonnet-20241022": {"input": 3.0, "output": 15.0, "cached_input": 0.30},
    "claude-3-5-haiku-20241022": {"input": 0.80, "output": 4.0, "cached_input": 0.08},
    "claude-3-opus-20240229": {"input": 15.0, "output": 75.0, "cached_input": 1.50},
    # Anthropic — current families (added 2026-08-21).
    "claude-fable-5": {"input": 10.0, "output": 50.0, "cached_input": 1.0},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cached_input": 0.50},
    "claude-opus-4-8": {"input": 5.0, "output": 25.0, "cached_input": 0.50},
    "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cached_input": 0.50},
    "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cached_input": 0.50},
    "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cached_input": 0.50},
    # Corrected 2026-09-02: was $2.00/$10.00, which was the launch price and
    # stopped being true on 2026-09-01. Rung 1 knows the date; this row is the
    # post-change rate, because a run old enough to want the $2.00 rate is old
    # enough that rung 1 will have priced it already.
    "claude-sonnet-5": {"input": 3.0, "output": 15.0, "cached_input": 0.30},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cached_input": 0.30},
    "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cached_input": 0.30},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cached_input": 0.10},
    # OpenAI
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4.1": {"input": 2.0, "output": 8.0},
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "o3": {"input": 2.0, "output": 8.0},
    "o3-mini": {"input": 1.10, "output": 4.40},
    "o4-mini": {"input": 1.10, "output": 4.40},
    "codex-mini-latest": {"input": 1.50, "output": 6.0},
    # Google
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0},
    # Corrected 2026-09-02: was $0.15/$0.60. The real rate has been $0.30/$2.50;
    # the output side was out by more than 4x.
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},
    "gemini-2.0-flash": {"input": 0.10, "output": 0.40},
}


def _estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cached_input_tokens: int = 0,
) -> float | None:
    """Estimate USD cost from ``_PRICING``. Returns None if the model is unknown.

    RUNG 3 of ``price_run``'s ladder, and a flat undated rate — call ``price_run``
    instead unless you specifically want the table. Kept public-shaped and
    unchanged in signature because both the runtime tracker and the cloud meter
    have imported it by name since 2026-06.

    ``input_tokens`` is INCLUSIVE of ``cached_input_tokens``; the cached portion
    is subtracted here and re-priced at the cache rate.
    """
    pricing = _PRICING.get(model)
    if not pricing:
        # No exact row. Match in both directions — "gpt-4o-2024-11-20" starts
        # with the key "gpt-4o", and "claude-sonnet-4-6" is a prefix of the dated
        # key it stands for — but take the LONGEST match rather than the first in
        # insertion order. First-hit made "gpt-4.1-mini-2025-04-14" resolve to
        # "gpt-4.1" and bill it at 5x, and made every price silently dependent on
        # the order rows happened to be written in.
        best: str | None = None
        for key in _PRICING:
            if (model.startswith(key) or key.startswith(model)) and (
                best is None or len(key) > len(best)
            ):
                best = key
        if best is not None:
            pricing = _PRICING[best]
    if not pricing:
        return None

    cost = (
        max(0, input_tokens - cached_input_tokens) * pricing["input"]
        + output_tokens * pricing["output"]
        + cached_input_tokens * pricing.get("cached_input", pricing["input"])
    ) / 1_000_000
    return round(cost, 6)


def _nonneg(value: object) -> int:
    """Coerce a token count to a non-negative int, tolerating None and strings."""
    try:
        n = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _model_refs(model: str) -> list[str]:
    """The forms of ``model`` worth trying against the price library, in order.

    Model strings reach the meter unnormalised and no single spelling can be
    assumed. ``deepseek-v3.2`` and ``deepseek-chat`` both resolve as given;
    ``deepseek/deepseek-chat`` does not, and neither does any other
    ``provider/name`` form, so the last path segment is tried too. The library
    already tolerates the ``[1m]`` context suffix this codebase appends, so that
    needs no rung of its own.
    """
    refs = [model]
    tail = model.rpartition("/")[2]
    if tail and tail != model:
        refs.append(tail)
    return refs


#: The deployment's own price list, installed by ``set_price_provider``. ``None``
#: on OSS and anywhere nothing registered one, which is why every rung below it
#: has to keep working.
_PRICE_PROVIDER: PriceProvider | None = None


def set_price_provider(provider: PriceProvider | None) -> None:
    """Install (or clear, with ``None``) the deployment's own price list.

    THE PROBLEM THIS SOLVES. Every rung below is somebody else's price list —
    genai-prices ships a maintained public one, and ``_PRICING`` is a hand copy of
    published rates. Neither can know what WE pay. A model served through our
    LiteLLM proxy under a negotiated rate bills at list here, and a model that only
    exists on our proxy — a fine-tune, an alias, a self-hosted weight — does not
    appear in any public list at all, so it prices ``None`` and bills zero. That is
    a bill we fail to send, and until 2026-09-02 it did not even log.

    So this rung goes ABOVE the public lists rather than filling their gaps. The
    proxy's configured cost is our actual cost basis, and where the two disagree
    the proxy is the one that is right about us.

    THE COST, stated because it is real: a proxy price is CURRENT, not
    effective-dated. Rungs 1 and 2 price a run at the rate in force when it ran,
    which matters because the metering sweeper drains a backlog that can span days.
    A run priced here gets today's rate. That is the trade the deployment makes by
    registering a provider, and it is the right one when the alternative is a
    confidently wrong public price or no price at all.

    The provider is called on the billing path and MUST NOT raise; anything it
    throws is swallowed and treated as a miss. It returns ``Decimal | None``, where
    ``None`` means "not my model" and falls through to the rungs below.
    """
    global _PRICE_PROVIDER
    _PRICE_PROVIDER = provider


def _price_via_provider(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    at: datetime,
) -> Decimal | None:
    """One registered-provider lookup. Returns None on any miss; never raises."""
    provider = _PRICE_PROVIDER
    if provider is None:
        return None
    try:
        return provider(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            at=at,
        )
    except Exception:  # noqa: BLE001 — a run must not die over its own invoice
        logger.debug("price_run: registered price provider failed for %r", model, exc_info=True)
        return None


def price_run(
    model: str | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    at: datetime | None,
) -> Decimal | None:
    """Price one run in USD, at the rate in force when it RAN.

    ``None`` means "could not price" and is never interchangeable with
    ``Decimal("0")`` — the caller has to distinguish an unknown bill from a zero
    one. This function never raises; a bad model id, a malformed count or a
    missing library all degrade to ``None``.

    ``input_tokens`` is the INCLUSIVE prompt total: cache reads and writes are
    subsets of it and both pricing rungs subtract them internally. Handing over
    an already-reduced remainder removes those tokens a second time.

    ``at`` is the run's own moment and is REQUIRED, though it may be ``None``.
    Prices are effective-dated: ``claude-sonnet-5`` was $2.00/MTok through
    2026-08-31 and $3.00 from 2026-09-01, and the metering sweeper bills up to
    200 runs a tick from a backlog that can span days. Pricing at "now" would
    reprice every backlogged run at today's rate. ``None`` says the caller
    genuinely has no moment; it is logged and falls back to now explicitly,
    rather than being defaulted silently inside the library.
    """
    if not model:
        return None

    inp = _nonneg(input_tokens)
    out = _nonneg(output_tokens)
    read = _nonneg(cache_read_tokens)
    write = _nonneg(cache_write_tokens)
    # The library REJECTS cache buckets that exceed their own total. A payload
    # that violates the inclusive contract is malformed rather than free, so widen
    # the total to cover its parts instead of raising or dropping the cache lines.
    if read + write > inp:
        logger.debug(
            "price_run: %r reported cache read+write (%d) above its inclusive "
            "input (%d) — widening the prompt total to cover it",
            model,
            read + write,
            inp,
        )
        inp = read + write

    if at is None:
        logger.warning(
            "price_run: no run timestamp for %r — pricing at now(), which is "
            "wrong for any run billed out of a backlog. Pass the run's own "
            "ended_at / createdAt.",
            model,
        )
        at = datetime.now(tz=UTC)
    elif at.tzinfo is None:
        # Mongo hands back naive stamps; the whole codebase reads those as UTC.
        at = at.replace(tzinfo=UTC)

    # Rung 0 — the deployment's own price list, when one has been registered.
    # See ``set_price_provider``: on cloud this is the LiteLLM proxy's configured
    # per-model cost, which is what we are actually charged, and it is the only
    # rung that knows about a custom model or a negotiated rate.
    priced = _price_via_provider(model, inp, out, read, write, at)
    if priced is not None:
        return priced

    for ref in _model_refs(model):
        priced = _price_via_library(ref, inp, out, read, write, at)
        if priced is not None:
            return priced

    # Rung 3 — the flat, undated table. It has no cache-WRITE column, so writes
    # fold into the read bucket here and undercount slightly; named rather than
    # hidden, and it only applies to ids rung 1 does not carry at all.
    table = _estimate_cost(model, inp, out, read + write)
    return None if table is None else Decimal(str(table))


def _price_via_library(
    ref: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    at: datetime,
) -> Decimal | None:
    """One ``genai-prices`` lookup. Returns None on any miss; never raises.

    Every failure mode is a miss, not an error: an unknown id raises
    ``LookupError``, malformed counts raise ``ValueError``, and an install
    without the package raises ``ImportError``. None of them may reach a caller
    that is in the middle of billing a run.
    """
    try:
        from genai_prices import calc_price
        from genai_prices.types import Usage
    except Exception:  # noqa: BLE001 — an absent price library is a miss
        logger.debug("price_run: genai-prices is not importable", exc_info=True)
        return None

    try:
        calc = calc_price(
            Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            ),
            model_ref=ref,
            genai_request_timestamp=at,
        )
    except Exception:  # noqa: BLE001 — every lookup failure is a miss
        return None

    price = getattr(calc, "total_price", None)
    return price if isinstance(price, Decimal) else None


@dataclass
class UsageRecord:
    """Single usage record for one agent turn."""

    timestamp: str
    backend: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    session_id: str = ""


@dataclass
class UsageSummary:
    """Aggregated usage stats."""

    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cached_input_tokens: int = 0
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    request_count: int = 0
    by_model: dict = field(default_factory=dict)
    by_backend: dict = field(default_factory=dict)


class BudgetExhaustedError(RuntimeError):
    """Raised by UsageTracker.record() when the monthly budget cap is hit."""


class UsageTracker:
    """Append-only usage tracker with JSONL persistence."""

    def __init__(self, path: Path | None = None):
        if path is None:
            from pocketpaw.config import get_config_dir

            path = get_config_dir() / "usage.jsonl"
        self._path = path
        self._lock = threading.Lock()

    def record(
        self,
        backend: str,
        model: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        session_id: str = "",
        total_cost_usd: float | None = None,
    ) -> UsageRecord:
        """Record a usage entry and persist to disk.

        If total_cost_usd is provided (e.g. from Claude Agent SDK's
        ResultMessage), it is used as the authoritative cost. Otherwise
        we estimate from the pricing table.
        """
        # total_tokens must include cached_input_tokens — they are real tokens
        # processed by the model even though billed at a lower rate.
        total = input_tokens + output_tokens + cached_input_tokens
        cost = (
            total_cost_usd
            if total_cost_usd is not None
            else _estimate_cost(model, input_tokens, output_tokens, cached_input_tokens)
        )

        record = UsageRecord(
            timestamp=datetime.now(tz=UTC).isoformat(),
            backend=backend,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_input_tokens,
            total_tokens=total,
            cost_usd=cost,
            session_id=session_id,
        )

        # Log a warning for unknown models so operators know to add pricing.
        # Primary budget enforcement is the async preflight in AgentLoop which
        # calls get_budget_snapshot() via asyncio.to_thread() before routing.
        # record() must never do blocking I/O on the event loop.
        if cost is None:
            logger.warning(
                "Unknown model pricing for '%s' — cost estimate unavailable. "
                "Add the model to _PRICING or supply total_cost_usd.",
                model,
            )

        try:
            with self._lock:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._path, "a") as f:
                    f.write(json.dumps(asdict(record)) + "\n")
        except Exception as e:
            logger.warning("Failed to write usage record: %s", e)

        return record

    def get_records(self, limit: int = 100) -> list[UsageRecord]:
        """Read recent records (newest first)."""
        if not self._path.exists():
            return []
        records: list[UsageRecord] = []
        try:
            lines = self._path.read_text().strip().split("\n")
            for line in reversed(lines):
                if len(records) >= limit:
                    break
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    records.append(UsageRecord(**data))
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Failed to read usage records: %s", e)
        return records

    def _iter_all_records(self) -> list[UsageRecord]:
        """Read ALL records from disk without any limit.

        Used internally by get_summary() to ensure aggregations are always
        computed over the full dataset, not just the most recent N records.
        """
        if not self._path.exists():
            return []
        records: list[UsageRecord] = []
        try:
            for line in self._path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    records.append(UsageRecord(**data))
                except Exception:
                    pass
        except Exception as e:
            logger.warning("Failed to read usage records: %s", e)
        return records

    def get_summary(self, since: str | None = None) -> dict:
        """Get aggregated usage summary, optionally filtered by timestamp.

        Uses _iter_all_records() so the summary covers every record ever
        written, not just the most recent 10 000.
        """
        records = self._iter_all_records()
        if since:
            records = [r for r in records if r.timestamp >= since]

        summary = UsageSummary()
        for r in records:
            summary.total_input_tokens += r.input_tokens
            summary.total_output_tokens += r.output_tokens
            summary.total_cached_input_tokens += r.cached_input_tokens
            summary.total_tokens += r.total_tokens
            if r.cost_usd is not None:
                summary.total_cost_usd += r.cost_usd
            summary.request_count += 1

            # By model
            if r.model:
                m = summary.by_model.setdefault(
                    r.model, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "count": 0}
                )
                m["input_tokens"] += r.input_tokens
                m["output_tokens"] += r.output_tokens
                if r.cost_usd is not None:
                    m["cost_usd"] += r.cost_usd
                m["count"] += 1

            # By backend
            b = summary.by_backend.setdefault(
                r.backend, {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "count": 0}
            )
            b["input_tokens"] += r.input_tokens
            b["output_tokens"] += r.output_tokens
            if r.cost_usd is not None:
                b["cost_usd"] += r.cost_usd
            b["count"] += 1

        summary.total_cost_usd = round(summary.total_cost_usd, 6)
        return asdict(summary)

    def clear(self) -> None:
        """Clear all usage records."""
        try:
            with self._lock:
                if self._path.exists():
                    self._path.write_text("")
        except Exception as e:
            logger.warning("Failed to clear usage records: %s", e)


# Singleton
_tracker: UsageTracker | None = None


def get_usage_tracker() -> UsageTracker:
    global _tracker
    if _tracker is None:
        _tracker = UsageTracker()
    return _tracker
