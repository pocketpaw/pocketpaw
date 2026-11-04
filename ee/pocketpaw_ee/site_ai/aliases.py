# ee/pocketpaw_ee/site_ai/aliases.py — model aliases and what a call may cost.
#
# A site names an alias (``fast``, ``smart``, ``vision``, ``embed``); this module
# maps it to the LiteLLM model in settings (``site_ai_model_<alias>``), its output
# and context caps, and prices token counts in micro-credits through the same
# ``usage_tracker.price_run`` + RateCard the wallet meter uses.
#
# ``reserve_micro`` is the worst case a call is held against: max(the dated price,
# the alias's fallback price) over the input estimate plus the full output cap, so
# a model the price table doesn't know still reserves a real figure. ``actual_micro``
# prices what the provider reported, falling back to the same fallback price when
# the table can't, so site counters never record a free call. Never log or return
# ``provider_model``: it is the one place the provider id exists.

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


@dataclass(frozen=True)
class AliasSpec:
    name: str
    setting: str
    max_output_tokens: int
    context_tokens: int
    vision: bool
    fallback_in_per_m: Decimal  # USD per million input tokens
    fallback_out_per_m: Decimal  # USD per million output tokens


ALIASES: dict[str, AliasSpec] = {
    "fast": AliasSpec("fast", "site_ai_model_fast", 4_096, 32_000, False, Decimal(1), Decimal(5)),
    "smart": AliasSpec(
        "smart", "site_ai_model_smart", 8_192, 64_000, False, Decimal(3), Decimal(15)
    ),
    "vision": AliasSpec(
        "vision", "site_ai_model_vision", 4_096, 32_000, True, Decimal(1), Decimal(5)
    ),
    "embed": AliasSpec("embed", "site_ai_model_embed", 0, 8_192, False, Decimal("0.2"), Decimal(0)),
}
CHAT_ALIASES = ("fast", "smart", "vision")


def _settings():
    from pocketpaw.config import get_settings

    return get_settings()


def provider_model(alias: str) -> str:
    """The LiteLLM model behind ``alias``; "" when unset (the alias is offline)."""
    spec = ALIASES[alias]
    return str(getattr(_settings(), spec.setting, "") or "").strip()


def _rate_card():
    from pocketpaw_ee.cloud.metering.service import load_rate_card

    return load_rate_card()


def _fallback_usd(spec: AliasSpec, input_tokens: int, output_tokens: int) -> Decimal:
    return (
        Decimal(input_tokens) * spec.fallback_in_per_m
        + Decimal(output_tokens) * spec.fallback_out_per_m
    ) / Decimal(1_000_000)


def _priced_usd(alias: str, input_tokens: int, output_tokens: int) -> Decimal | None:
    from pocketpaw.usage_tracker import price_run

    model = provider_model(alias)
    if not model:
        return None
    return price_run(
        model, input_tokens=input_tokens, output_tokens=output_tokens, at=datetime.now(UTC)
    )


def reserve_micro(alias: str, input_tokens: int, output_tokens: int) -> int:
    spec = ALIASES[alias]
    priced = _priced_usd(alias, input_tokens, output_tokens) or Decimal(0)
    usd = max(priced, _fallback_usd(spec, input_tokens, output_tokens))
    # +1 so a rounding step can never make the hold smaller than the settle.
    return _rate_card().to_micro_credits(usd) + 1


def actual_micro(alias: str, input_tokens: int, output_tokens: int) -> int:
    if input_tokens <= 0 and output_tokens <= 0:
        return 0
    spec = ALIASES[alias]
    priced = _priced_usd(alias, input_tokens, output_tokens)
    usd = priced if priced is not None and priced > 0 else None
    if usd is None:
        usd = _fallback_usd(spec, input_tokens, output_tokens)
    return _rate_card().to_micro_credits(usd)


__all__ = [
    "ALIASES",
    "CHAT_ALIASES",
    "AliasSpec",
    "actual_micro",
    "provider_model",
    "reserve_micro",
]
