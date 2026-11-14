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
# ``price_usd``: the verified table rate (``usage_tracker._PRICING``, exact id or a
# dated suffix, provider prefixes stripped, plus ``_LONG_CONTEXT`` tiers) as
# (uncached + cache reads x read rate + writes x 1.25) x input rate + output x
# output rate; a model with no row is priced by the deployment's existing ladder
# (its proxy price list, then genai-prices). ``cost_usd`` prices what ran (the
# model the backend reports, else the one asked for): from tokens on the Claude
# Agent SDK, whose reported ``total_cost_usd`` is not trusted (the CLI's figure
# for Haiku 5.5 fit Opus rates); every other backend is billed its reported cost
# first, as the meter's ``resolve_cost`` does. ``reserve_micro`` holds the input estimate
# plus ``SDK_OVERHEAD_TOKENS``, all written to cache, plus the full output cap.
# Nothing pricing a model raises ``Unpriced``: refused, never billed at a guess.
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


ALIASES: dict[str, AliasSpec] = {
    "fast": AliasSpec("fast", 4_096, 150_000, False),
    "smart": AliasSpec("smart", 8_192, 150_000, False),
    "vision": AliasSpec("vision", 4_096, 150_000, True),
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


class Unpriced(Exception):
    """A model with no verified price: site AI refuses to run or guess it."""

    def __init__(self, model: str | None) -> None:
        self.model = model
        super().__init__(model or "<none>")


# Prompt tokens the Claude CLI adds around the site's own prompt (its prefix and
# attribution blocks), held so the hold never sits under the real prompt size.
SDK_OVERHEAD_TOKENS = 200
# Anthropic's cache multipliers on the input rate: a 5-minute write is 1.25x; a
# read is the table's ``cached_input`` rate, else 0.1x.
CACHE_WRITE_RATE = Decimal("1.25")
CACHE_READ_RATE = Decimal("0.1")
# Long-context tiers the flat table cannot hold: past ``threshold`` prompt tokens
# the WHOLE request bills at the higher rates (cache rates scale with input).
_LONG_CONTEXT: dict[str, tuple[int, Decimal, Decimal]] = {
    "claude-haiku-5-5": (100_000, Decimal("0.50"), Decimal("2.50")),
}


def _spellings(model: str) -> list[str]:
    """``model`` and the forms a provider prefix or context suffix hides it under
    (``anthropic:x``, ``litellm/anthropic/x``, ``x[1m]``), most specific first."""
    out = [model]
    for form in list(out):
        bare = form.removesuffix("[1m]")
        if ":" in bare:
            bare = bare.split(":", 1)[1]
        bare = bare.rpartition("/")[2]
        if bare and bare not in out:
            out.append(bare)
    return out


def _table_key(model: str | None) -> str | None:
    """The verified table row for ``model`` (any ``_spellings`` form): an exact id,
    or the longest row it extends with a dated suffix (``claude-haiku-4-5-20251001``).
    Never a looser match, so ``claude`` or a renamed model has no row."""
    from pocketpaw.usage_tracker import _PRICING

    if not model:
        return None
    for form in _spellings(model):
        if form in _PRICING:
            return form
        rows = [key for key in _PRICING if form.startswith(key + "-")]
        if rows:
            return max(rows, key=len)
    return None


def _ladder_usd(
    model: str, prompt: int, output_tokens: int, read: int, write: int
) -> Decimal | None:
    """A model with no table row, priced by the deployment's existing ladder:
    its own price list (the LiteLLM proxy's costs), then genai-prices with and
    without a provider prefix. The flat table's loose prefix match is skipped;
    ``_table_key`` already answered for the table."""
    from datetime import UTC, datetime

    from pocketpaw import usage_tracker

    at = datetime.now(UTC)
    for form in _spellings(model):
        priced = usage_tracker._price_via_provider(form, prompt, output_tokens, read, write, at)
        if priced is not None and priced > 0:
            return priced
        for ref in usage_tracker._model_refs(form):
            priced = usage_tracker._price_via_library(ref, prompt, output_tokens, read, write, at)
            if priced is not None and priced > 0:
                return priced
    return None


def price_usd(
    model: str | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> Decimal | None:
    """Provider cost of these tokens: the verified table rate for ``model``, else
    the existing price ladder (``_ladder_usd``); None when nothing prices it.
    ``input_tokens`` is the inclusive prompt total. At the table rate: (uncached +
    read x read rate + write x write rate) x input rate, plus output x output
    rate, at the long-context rates past a model's tier."""
    from pocketpaw.usage_tracker import _PRICING

    prompt = max(0, int(input_tokens))
    read, write = max(0, int(cache_read)), max(0, int(cache_write))
    prompt = max(prompt, read + write)
    output_tokens = max(0, int(output_tokens))
    key = _table_key(model)
    if key is None:
        return _ladder_usd(model, prompt, output_tokens, read, write) if model else None
    row = _PRICING[key]
    rate_in = Decimal(str(row["input"]))
    rate_out = Decimal(str(row["output"]))
    rate_read = Decimal(str(row["cached_input"])) if "cached_input" in row else None
    tier = _LONG_CONTEXT.get(key)
    if tier is not None and prompt > tier[0]:
        scale = tier[1] / rate_in
        rate_in, rate_out = tier[1], tier[2]
        rate_read = rate_read * scale if rate_read is not None else None
    if rate_read is None:
        rate_read = rate_in * CACHE_READ_RATE
    plain = prompt - read - write
    usd = (
        Decimal(plain) * rate_in
        + Decimal(read) * rate_read
        + Decimal(write) * rate_in * CACHE_WRITE_RATE
        + Decimal(output_tokens) * rate_out
    )
    return usd / Decimal(1_000_000)


def is_priced(model: str | None) -> bool:
    """Something can price ``model``: a table row or the ladder."""
    if _table_key(model) is not None:
        return True
    return bool(model) and price_usd(model, input_tokens=1_000_000, output_tokens=0) is not None


def reserve_micro(alias: str, model: str | None, input_tokens: int, output_tokens: int) -> int:
    """The hold: the whole prompt (plus the SDK's overhead) written to the cache
    and the full output cap, at ``model``'s verified rate. Raises ``Unpriced``."""
    prompt = max(0, int(input_tokens)) + SDK_OVERHEAD_TOKENS
    usd = price_usd(model, input_tokens=prompt, output_tokens=output_tokens, cache_write=prompt)
    if usd is None:
        raise Unpriced(model)
    # +1 so a rounding step can never make the hold smaller than the settle.
    return _rate_card().to_micro_credits(usd) + 1


def billed_model(model: str | None, usage: dict[str, Any] | None) -> str | None:
    """The model a call is priced as: the one the backend says answered, else the
    one that was asked for."""
    reported = str((usage or {}).get("model") or "").strip()
    return reported or model


# The backend whose reported ``total_cost_usd`` is not trusted: the Claude CLI's
# figure for Haiku 5.5 fit Opus 5.5 rates.
DISTRUSTED_COST_BACKENDS = frozenset({"claude_agent_sdk"})


def cost_usd(
    alias: str,
    model: str | None,
    usage: dict[str, Any] | None,
    tokens: tuple[int, int],
    *,
    backend: str | None = None,
) -> Decimal:
    """Provider cost (before markup) of one call by the model that ran
    (``billed_model``): from its TOKENS at ``price_usd``. Any backend but the
    Claude Agent SDK is billed its reported ``total_cost_usd`` first, as the
    meter's ``resolve_cost`` does; the SDK's figure is ignored. ``tokens`` is
    (inclusive input, output) and prices a call whose usage carries no counts.
    Raises ``Unpriced`` when nothing prices the model."""
    from pocketpaw_ee.cloud.metering.service import _prompt_tokens

    input_tokens, output_tokens = tokens
    if input_tokens <= 0 and output_tokens <= 0:
        return Decimal(0)
    usage = usage or {}
    ran = billed_model(model, usage)
    if backend not in DISTRUSTED_COST_BACKENDS:
        # The meter's first rung: a backend that reports a real cost is billed it.
        reported = usage.get("total_cost_usd")
        if isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported > 0:
            return Decimal(str(reported))
    if usage.get("input_tokens") or usage.get("output_tokens"):
        prompt, read, write = _prompt_tokens(usage)
        output_tokens = max(0, int(usage.get("output_tokens") or 0))
    else:
        prompt, read, write = input_tokens, 0, 0
    usd = price_usd(
        ran,
        input_tokens=prompt,
        output_tokens=output_tokens,
        cache_read=read,
        cache_write=write,
    )
    if usd is None:
        raise Unpriced(ran)
    return usd


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
    "Unpriced",
    "billed_model",
    "is_priced",
    "mark_offline",
    "price_usd",
    "reserve_micro",
    "to_micro",
]
