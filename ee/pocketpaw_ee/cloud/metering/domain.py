# ee/pocketpaw_ee/cloud/metering/domain.py — frozen value objects for the
# compute-cost metering entity (BC-3, the Meter + Price primitives).
#
# Pure-Python, framework-free shapes. The service hands these back across the
# entity boundary instead of leaking the Beanie ``ChatRunDoc`` or raw dicts.
#
#   * ``RateCard`` — the declarative Price primitive: a flat USD-cost markup +
#     the per-credit USD denomination. ``to_credits(cost_usd)`` is the rate-card
#     conversion (round(cost_usd * markup / credit_usd)). Kept a dataclass so a
#     tiered card (per-model / per-tier rates) can subclass or replace it later
#     without touching the debit path.
#   * ``ComputeCost`` — the Meter primitive's output: the resolved USD cost of a
#     run plus the model it was attributed to and the source it came from
#     (reported total_cost_usd vs. the pricing-table estimate vs. none).
#
# Created 2026-06-24 (integration/billing-credits, BC-3): new entity.

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Where a run's resolved cost came from — for the bill ref + debug logging.
CostSource = Literal["reported", "estimated", "none"]


@dataclass(frozen=True)
class RateCard:
    """The compute-spend rate card (the Price primitive).

    ``markup`` is the flat multiplier applied to a run's real USD compute cost;
    ``credit_usd`` is the USD value of one credit (1 credit == $0.01). The
    conversion to billable credits is ``round(cost_usd * markup / credit_usd)``
    — with the defaults that is ``round(cost_usd * 250)``.

    Declarative on purpose: a flat card is two numbers, but the shape leaves room
    for a tiered card (e.g. per-model multipliers) without changing ``bill_run``.
    """

    markup: float
    credit_usd: float

    def to_credits(self, cost_usd: float) -> int:
        """Convert a USD compute cost into integer credits via the rate card.

        ``round`` (banker's rounding) is fine here — the amounts are tiny and the
        markup absorbs sub-credit drift. A non-positive cost yields 0 credits (no
        debit; the run is still marked billed so it isn't re-swept).
        """
        if cost_usd <= 0:
            return 0
        return round(cost_usd * self.markup / self.credit_usd)


@dataclass(frozen=True)
class ComputeCost:
    """A run's resolved compute cost (the Meter primitive's output).

    ``cost_usd`` is the authoritative USD cost; ``source`` records how it was
    resolved (the backend's reported ``total_cost_usd``, the pricing-table
    estimate, or none for an unknown model / empty usage). ``model`` is the model
    the cost was attributed to (``None`` when usage carried no model).
    """

    cost_usd: float
    source: CostSource
    model: str | None
