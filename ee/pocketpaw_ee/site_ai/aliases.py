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
# ``reserve_micro`` is the worst case a call is held against: max(the dated price of
# the model that will answer (the owner's pick, else the agent's own, else its
# backend's default), the alias's fallback rate) over the input estimate plus the
# full output cap. ``cost_usd`` prices what a call used through the meter's own
# ``resolve_cost`` (the backend's reported cost, else the dated price of the model
# that ran), falling back to the same rate so site counters never record a free call.
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


def _fallback_usd(alias: str, input_tokens: int, output_tokens: int) -> Decimal:
    spec = ALIASES[alias]
    return (
        Decimal(input_tokens) * spec.fallback_in_per_m
        + Decimal(output_tokens) * spec.fallback_out_per_m
    ) / Decimal(1_000_000)


def reserve_micro(alias: str, model: str | None, input_tokens: int, output_tokens: int) -> int:
    from pocketpaw.usage_tracker import price_run

    priced = None
    if model:
        priced = price_run(
            model, input_tokens=input_tokens, output_tokens=output_tokens, at=datetime.now(UTC)
        )
    usd = max(priced or Decimal(0), _fallback_usd(alias, input_tokens, output_tokens))
    # +1 so a rounding step can never make the hold smaller than the settle.
    return _rate_card().to_micro_credits(usd) + 1


def cost_usd(
    alias: str, model: str | None, usage: dict[str, Any] | None, tokens: tuple[int, int]
) -> Decimal:
    """Provider cost (before markup) of one call: the meter's ``resolve_cost`` over
    the backend's usage (its reported cost, else the dated price of ``model``), else
    the alias's fallback rate over ``tokens`` (input, output)."""
    from pocketpaw_ee.cloud.metering.service import resolve_cost

    input_tokens, output_tokens = tokens
    if input_tokens <= 0 and output_tokens <= 0:
        return Decimal(0)
    priced = dict(usage or {})
    priced.update(model=model, input_tokens=input_tokens, output_tokens=output_tokens)
    try:
        exact = resolve_cost(priced, at=datetime.now(UTC)).cost_usd_exact
        if exact is not None and exact > 0:
            return Decimal(exact)
    except Exception:  # noqa: BLE001 - a call must not die over its own price
        logger.warning("site_ai: resolve_cost failed; using the fallback rate", exc_info=True)
    return _fallback_usd(alias, input_tokens, output_tokens)


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
    "reserve_micro",
    "to_micro",
]
