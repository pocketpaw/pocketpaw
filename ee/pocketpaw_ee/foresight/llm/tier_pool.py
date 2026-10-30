# ee/pocketpaw_ee/foresight/llm/tier_pool.py
# Created: 2026-05-25 (feat/foresight-v03-calibration) — RFC 08 PR 3.
#
# Tier-pool builder — RFC 08 §7.3 + §10.
#
# Foresight ticks fan out to N personas; each persona round-robins
# through a ``list[BaseModelBackend]`` whose composition matches the
# captain-locked tier mix:
#
#   premium (Sonnet 4.7)        5%  — strategic / approval personas
#   mid     (Haiku 4.7)        15%  — mid-fidelity persona cohort
#   tail    (Llama-3.1-8B vLLM) 80% — synthesized / market-sim long tail
#
# This module provides:
#
#   - ``TierMix``: a frozen dataclass guarding the {premium, mid, tail}
#     triple. Sums to 1.0; per-tier overrides land via per-scenario
#     YAML (RFC 08 §18 ``tier_mix:`` block).
#   - ``build_tier_pool(...)``: constructs a ``list[BaseModelBackend]``
#     of length N where consecutive entries span tiers (avoids
#     activation-correlated cluster effects). Caller-supplied factories
#     for each tier produce backend instances on demand.
#   - ``tier_distribution(...)``: telemetry helper for the cost meter
#     and the per-run report (tier × count × wall-clock).
#
# vLLM hosting is DEFERRED to v2.0 (per RFC 08 §10 cost-model lock).
# PR 3's tier-pool builder declares the routing — the tail factory may
# return a ``LiteLLMFallbackBackend`` pointed at Modal-hosted Llama or
# a local Ollama instance for dev; the contract is identical.

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# --- Tier mix --------------------------------------------------------


@dataclass(frozen=True)
class TierMix:
    """Captain-locked default: 5 / 15 / 80 (RFC 08 §10).

    Overridable per scenario YAML in the ``tier_mix:`` block. Out-of-
    default mixes trigger a cost-estimator warning (PR 6 work — UI).

    All three fields are floats in [0.0, 1.0]; they must sum to 1.0
    (within a small float-precision band).
    """

    premium: float = 0.05
    mid: float = 0.15
    tail: float = 0.80

    def __post_init__(self) -> None:
        for name in ("premium", "mid", "tail"):
            value = getattr(self, name)
            if not isinstance(value, (int, float)):
                raise TypeError(f"TierMix.{name} must be a number, got {type(value).__name__}")
            if value < 0.0 or value > 1.0:
                raise ValueError(f"TierMix.{name} must be in [0.0, 1.0], got {value}")
        total = self.premium + self.mid + self.tail
        if not (0.999 < total < 1.001):
            raise ValueError(
                f"TierMix must sum to 1.0 (within ±0.001), got {total:.4f} "
                f"(premium={self.premium}, mid={self.mid}, tail={self.tail})"
            )

    @classmethod
    def locked_default(cls) -> TierMix:
        """The RFC 08 §10 captain-locked default tier mix."""
        return cls()  # 0.05 / 0.15 / 0.80

    def is_default(self) -> bool:
        """``True`` for the locked default; ``False`` for any override.

        The cost estimator surfaces a warning when a scenario YAML
        declares a non-default mix — typically because the operator is
        opting into more Sonnet for higher fidelity at higher cost.
        """
        return (
            abs(self.premium - 0.05) < 0.0001
            and abs(self.mid - 0.15) < 0.0001
            and abs(self.tail - 0.80) < 0.0001
        )

    def as_dict(self) -> dict[str, float]:
        return {"premium": self.premium, "mid": self.mid, "tail": self.tail}


# --- Backend factory protocol ---------------------------------------


class BackendFactory(Protocol):
    """A no-arg callable that returns a fresh ``BaseModelBackend``-shaped
    object. The contract is intentionally loose so any of:

      - ``lambda: ClaudeCodeBackend(model="claude-sonnet-4-7")``
      - ``lambda: LiteLLMFallbackBackend(model="claude-haiku-4-7")``
      - ``lambda: VLLMBackend(endpoint="http://localhost:8000")``

    can serve as a factory. Foresight only requires ``async def run(...)
    -> dict`` (the CAMEL ``BaseModelBackend`` surface) — the
    ``async def complete(prompt: str) -> str`` shorthand is preserved
    for the v0.1 smoke loop.
    """

    def __call__(self) -> Any: ...  # pragma: no cover — protocol


# --- Tier pool construction -----------------------------------------


def build_tier_pool(
    *,
    tier_mix: TierMix,
    n_personas: int,
    premium_factory: BackendFactory,
    mid_factory: BackendFactory,
    tail_factory: BackendFactory,
    seed: int | None = None,
) -> list[Any]:
    """Build a list of ``BaseModelBackend`` instances honoring the tier mix.

    The pool's length equals ``n_personas`` so a caller can assign
    persona i to ``pool[i]`` without overflow. Composition:

      - ``round(n_personas * tier_mix.premium)`` premium backends
      - ``round(n_personas * tier_mix.mid)`` mid backends
      - everything else is tail (preserves sum == n_personas under
        any rounding drift)

    The pool is then *pre-shuffled* (deterministically when ``seed`` is
    set) so consecutive persona ids span tiers — avoiding cluster
    effects when activation is correlated with persona id (e.g.
    "personas 0-99 fire on tick 1, 100-199 on tick 2…").

    Args:
        tier_mix: the {premium, mid, tail} fractions.
        n_personas: total persona count.
        premium_factory, mid_factory, tail_factory: callables returning
            fresh backend instances per persona slot.
        seed: optional RNG seed for the shuffle. ``None`` uses
            ``random.shuffle``'s default (non-deterministic across
            processes); tests + reproducible runs should pass a seed.

    Returns:
        ``list[BaseModelBackend]`` of length ``n_personas``.

    Raises:
        ValueError: if ``n_personas < 1`` or any factory returns None.
    """
    if n_personas < 1:
        raise ValueError(f"n_personas must be >= 1, got {n_personas}")

    n_premium = round(n_personas * tier_mix.premium)
    n_mid = round(n_personas * tier_mix.mid)
    n_tail = n_personas - n_premium - n_mid
    if n_tail < 0:
        # Rounding pushed premium + mid past total. Trim mid down.
        n_mid = max(0, n_personas - n_premium)
        n_tail = n_personas - n_premium - n_mid

    pool: list[Any] = []
    pool.extend(_invoke_factory(premium_factory, "premium", n_premium))
    pool.extend(_invoke_factory(mid_factory, "mid", n_mid))
    pool.extend(_invoke_factory(tail_factory, "tail", n_tail))

    # Shuffle so persona-id-correlated activation hits all tiers.
    rng = random.Random(seed)
    rng.shuffle(pool)

    logger.debug(
        "Built tier pool: n_personas=%d premium=%d mid=%d tail=%d (mix=%s, seed=%s)",
        n_personas,
        n_premium,
        n_mid,
        n_tail,
        tier_mix.as_dict(),
        seed,
    )
    return pool


def _invoke_factory(factory: BackendFactory, tier_name: str, count: int) -> list[Any]:
    if count == 0:
        return []
    instances: list[Any] = []
    for i in range(count):
        try:
            instance = factory()
        except Exception as exc:  # noqa: BLE001 — surface tier-name in the error
            raise RuntimeError(
                f"tier-pool {tier_name} factory raised on slot {i + 1}/{count}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if instance is None:
            raise ValueError(
                f"tier-pool {tier_name} factory returned None on slot {i + 1}/{count}; "
                "factories must return a backend instance"
            )
        instances.append(instance)
    return instances


def tier_distribution(
    pool: list[Any],
    *,
    premium_marker: Callable[[Any], bool] | None = None,
    mid_marker: Callable[[Any], bool] | None = None,
) -> dict[str, int]:
    """Telemetry — count how many slots in ``pool`` are premium / mid /
    tail. Used by the cost meter and the per-run report.

    Tier detection uses caller-supplied predicates (the markers).
    Default predicates inspect the backend's ``_model`` attribute
    (set by ``ClaudeCodeBackend`` / ``LiteLLMFallbackBackend``
    constructors) — if absent, everything is counted as tail.

    Args:
        pool: the backend pool to inspect.
        premium_marker: predicate identifying premium-tier backends.
            Defaults to ``"sonnet" in (model or "").lower()``.
        mid_marker: predicate identifying mid-tier backends.
            Defaults to ``"haiku" in (model or "").lower()``.

    Returns:
        ``{"premium": n_premium, "mid": n_mid, "tail": n_tail}``.
    """

    def _default_premium(b: Any) -> bool:
        m = getattr(b, "_model", None) or ""
        return "sonnet" in str(m).lower()

    def _default_mid(b: Any) -> bool:
        m = getattr(b, "_model", None) or ""
        return "haiku" in str(m).lower()

    is_premium = premium_marker or _default_premium
    is_mid = mid_marker or _default_mid

    n_premium = 0
    n_mid = 0
    n_tail = 0
    for backend in pool:
        if is_premium(backend):
            n_premium += 1
        elif is_mid(backend):
            n_mid += 1
        else:
            n_tail += 1
    return {"premium": n_premium, "mid": n_mid, "tail": n_tail}
