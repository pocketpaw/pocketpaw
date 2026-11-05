# ee/pocketpaw_ee/site_ai/aliases.py — model aliases, what serves them, and what a
# call may cost.
#
# A site names an alias (``fast``, ``smart``, ``vision``). The owner maps each one to
# a workspace agent and, optionally, a catalog model (``SiteAiSettingsDoc.
# alias_models``); an unmapped alias runs on the workspace default agent with its own
# model. ``choice`` reads that mapping. Only the agent's backend and model are used,
# never its prompt, tools, skills or soul: the call goes through
# ``cloud.chat.side_completion`` isolated, with the site's own system prompt.
#
# ``reserve_micro`` is the worst case a call is held against: the input estimate
# plus ``SDK_OVERHEAD_TOKENS``, all written to the prompt cache (the Claude SDK
# reports its prompt as cache writes, billed above plain input), plus the full
# output cap, priced at max(the dated price of the model that will answer, the
# alias's fallback rate) times the model's learned ``rate_scale``. ``cost_usd``
# prices what a call used through the meter's own ``resolve_cost`` (the backend's
# reported cost, else the dated price), falling back to the alias rate so site
# counters never record a free call. ``observe`` compares that cost with our own
# estimate for the same tokens; a backend that reports more (its own price list)
# raises the model's ``rate_scale`` so the next hold covers it. The scale is per
# process, bounded to [1, ``RATE_SCALE_MAX``], and only ever errs high.
#
# Offline aliases: a call that fails because the alias is misconfigured (an unknown
# model, an agent that is gone) marks it offline for ``OFFLINE_TTL_S`` in this
# process. Admission refuses an offline alias without calling anything, and the
# owner and agent views list only online aliases. One ERROR line per alias per
# window, marked ``alias_misconfigured``; never a model id or agent text in it.

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)

OFFLINE_TTL_S = 300.0


@dataclass(frozen=True)
class AliasSpec:
    name: str
    max_output_tokens: int
    context_tokens: int
    vision: bool
    fallback_in_per_m: Decimal  # USD per million input tokens, when nothing prices it
    fallback_out_per_m: Decimal  # USD per million output tokens


ALIASES: dict[str, AliasSpec] = {
    "fast": AliasSpec("fast", 4_096, 150_000, False, Decimal(1), Decimal(5)),
    "smart": AliasSpec("smart", 8_192, 150_000, False, Decimal(3), Decimal(15)),
    "vision": AliasSpec("vision", 4_096, 150_000, True, Decimal(1), Decimal(5)),
}
CHAT_ALIASES = ("fast", "smart", "vision")


@dataclass(frozen=True)
class AliasChoice:
    """What serves one alias: ``agent_id`` None = the workspace default agent,
    ``model`` None = that agent's own model."""

    agent_id: str | None = None
    model: str | None = None


def choice(settings: Any, alias: str) -> AliasChoice:
    raw = (getattr(settings, "alias_models", None) or {}).get(alias) or {}
    agent_id = str(raw.get("agent_id") or "").strip() or None
    model = str(raw.get("model") or "").strip() or None
    return AliasChoice(agent_id=agent_id, model=model)


# ------------------------------------------------------------------ offline

_OFFLINE: dict[tuple[str, str, str], float] = {}


def mark_offline(workspace_id: str, pocket_id: str, alias: str, code: str | None) -> None:
    key = (workspace_id, pocket_id, alias)
    now = time.monotonic()
    if _OFFLINE.get(key, 0.0) <= now:
        logger.error(
            "site_ai: alias_misconfigured alias=%s pocket=%s code=%s (offline for %ds)",
            alias,
            pocket_id,
            code,
            int(OFFLINE_TTL_S),
        )
    _OFFLINE[key] = now + OFFLINE_TTL_S


def is_offline(workspace_id: str, pocket_id: str, alias: str) -> bool:
    key = (workspace_id, pocket_id, alias)
    until = _OFFLINE.get(key)
    if until is None:
        return False
    if until <= time.monotonic():
        _OFFLINE.pop(key, None)
        return False
    return True


def clear_offline(workspace_id: str, pocket_id: str) -> None:
    """The owner changed the mapping: give every alias a fresh try."""
    for key in [k for k in _OFFLINE if k[:2] == (workspace_id, pocket_id)]:
        _OFFLINE.pop(key, None)


# ------------------------------------------------------------------ prices


def _rate_card():
    from pocketpaw_ee.cloud.metering.service import load_rate_card

    return load_rate_card()


# Prompt tokens the Claude CLI adds around the site's own prompt (its prefix and
# attribution blocks), held so the hold never sits under the real prompt size.
SDK_OVERHEAD_TOKENS = 200
# Fallback-rate multipliers for cache traffic: a write at the 1-hour TTL rate (the
# dearer of the two), a read at a tenth of input.
CACHE_WRITE_RATE = Decimal(2)
CACHE_READ_RATE = Decimal("0.1")
RATE_SCALE_MAX = 50.0
_RATE_SCALE_KEYS = 256
# Learned ``reported cost / our estimate`` per price key; see ``observe``.
_RATE_SCALE: dict[str, float] = {}


def _fallback_usd(
    alias: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0
) -> Decimal:
    """The alias rate. ``input_tokens`` is the inclusive prompt total; its cached
    parts are priced at the cache rates."""
    spec = ALIASES[alias]
    plain = max(0, input_tokens - cache_read - cache_write)
    prompt = (
        Decimal(plain)
        + Decimal(cache_write) * CACHE_WRITE_RATE
        + Decimal(cache_read) * CACHE_READ_RATE
    )
    return (
        prompt * spec.fallback_in_per_m + Decimal(output_tokens) * spec.fallback_out_per_m
    ) / Decimal(1_000_000)


def _estimate_usd(
    alias: str,
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> Decimal:
    """Our own price for these tokens: max(the dated price of ``model``, the alias
    rate). ``input_tokens`` is the inclusive prompt total."""
    from pocketpaw.usage_tracker import price_run

    priced = None
    if model:
        priced = price_run(
            model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            at=datetime.now(UTC),
        )
    fallback = _fallback_usd(alias, input_tokens, output_tokens, cache_read, cache_write)
    return max(priced or Decimal(0), fallback)


def price_key(alias: str, model: str | None) -> str:
    """What a learned rate scale is kept under: the model, else the alias."""
    return model or f"alias:{alias}"


def rate_scale(alias: str, model: str | None) -> float:
    return _RATE_SCALE.get(price_key(alias, model), 1.0)


def reserve_micro(alias: str, model: str | None, input_tokens: int, output_tokens: int) -> int:
    """The hold: the whole prompt (plus the SDK's overhead) written to the cache
    and the full output cap, at our price times the model's learned scale."""
    prompt = max(0, int(input_tokens)) + SDK_OVERHEAD_TOKENS
    usd = _estimate_usd(alias, model, prompt, output_tokens, cache_write=prompt)
    usd *= Decimal(str(rate_scale(alias, model)))
    # +1 so a rounding step can never make the hold smaller than the settle.
    return _rate_card().to_micro_credits(usd) + 1


def _cache_parts(usage: dict[str, Any] | None) -> tuple[int, int]:
    usage = usage or {}
    read = max(0, int(usage.get("cache_read_tokens") or 0))
    write = max(0, int(usage.get("cache_write_tokens") or 0))
    return read, write


def cost_usd(
    alias: str, model: str | None, usage: dict[str, Any] | None, tokens: tuple[int, int]
) -> Decimal:
    """Provider cost (before markup) of one call: the meter's ``resolve_cost`` over
    the backend's usage (its reported cost, else the dated price of ``model``, else
    of the model the usage names), else the alias's fallback rate. ``tokens`` is
    (inclusive input, output); it prices a call whose usage carries no counts."""
    from pocketpaw_ee.cloud.metering.service import resolve_cost

    input_tokens, output_tokens = tokens
    if input_tokens <= 0 and output_tokens <= 0:
        return Decimal(0)
    priced = dict(usage or {})
    priced["model"] = model or priced.get("model")
    if not (priced.get("input_tokens") or priced.get("output_tokens")):
        # No counts from the backend: price the estimate. Cache fields would be
        # added on top of an inclusive total, so they go.
        for key in ("cache_read_tokens", "cache_write_tokens", "cached_input_tokens"):
            priced.pop(key, None)
        priced.update(input_tokens=input_tokens, output_tokens=output_tokens)
    try:
        exact = resolve_cost(priced, at=datetime.now(UTC)).cost_usd_exact
        if exact is not None and exact > 0:
            return Decimal(exact)
    except Exception:  # noqa: BLE001 - a call must not die over its own price
        logger.warning("site_ai: resolve_cost failed; using the fallback rate", exc_info=True)
    read, write = _cache_parts(usage)
    return _fallback_usd(alias, input_tokens, output_tokens, read, write)


def observe(
    alias: str,
    model: str | None,
    usage: dict[str, Any] | None,
    tokens: tuple[int, int],
    cost: Decimal,
) -> float:
    """Record what a call cost against our own estimate for its tokens; returns
    the ratio. A ratio above the key's scale raises it at once; a lower one lets
    it decay slowly (never below 1), so holds err high."""
    input_tokens, output_tokens = tokens
    read, write = _cache_parts(usage)
    estimate = _estimate_usd(alias, model, input_tokens, output_tokens, read, write)
    if estimate <= 0 or cost <= 0:
        return 0.0
    ratio = float(cost / estimate)
    key = price_key(alias, model)
    old = _RATE_SCALE.get(key, 1.0)
    new = ratio if ratio > old else 0.9 * old + 0.1 * ratio
    new = min(RATE_SCALE_MAX, max(1.0, new))
    if new == 1.0:
        _RATE_SCALE.pop(key, None)
        return ratio
    if key not in _RATE_SCALE and len(_RATE_SCALE) >= _RATE_SCALE_KEYS:
        _RATE_SCALE.pop(next(iter(_RATE_SCALE)))
    _RATE_SCALE[key] = new
    return ratio


def to_micro(usd: Decimal) -> int:
    return _rate_card().to_micro_credits(usd) if usd > 0 else 0


__all__ = [
    "ALIASES",
    "CHAT_ALIASES",
    "AliasChoice",
    "AliasSpec",
    "choice",
    "clear_offline",
    "cost_usd",
    "is_offline",
    "mark_offline",
    "observe",
    "price_key",
    "rate_scale",
    "reserve_micro",
    "to_micro",
]
