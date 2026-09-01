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
# Updated 2026-09-02 (fix/metering-dated-pricing): two changes, both about money
#   arriving as a Decimal now that ``usage_tracker.price_run`` returns one.
#   ``ComputeCost`` carries ``cost_usd_exact`` beside the float, and
#   ``to_credits`` does its arithmetic in Decimal rather than taking a float and
#   hoping. The float stays because it is what gets serialised onto the ledger
#   ref and ``BillResult``; the Decimal is what the conversion reads. Also a new
#   ``CostSource`` member, ``unpriced`` — see below.

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Literal

# Where a run's resolved cost came from — for the bill ref + debug logging.
#
# ``unpriced`` was split out of ``none`` on 2026-09-02. They used to be the same
# value, which is why an unpriced model was invisible: a run with real tokens on
# a model nothing could price looked identical to a run with no usage at all, and
# both logged at DEBUG. They are different failures. ``none`` is nothing to bill;
# ``unpriced`` is something to bill that we could not put a number on, and it is
# the one an operator has to see.
CostSource = Literal["reported", "estimated", "unpriced", "none"]


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

    def to_credits(self, cost_usd: Decimal | float) -> int:
        """Convert a USD compute cost into integer credits via the rate card.

        Accepts a ``Decimal`` since 2026-09-02 — that is what
        ``usage_tracker.price_run`` returns, and routing it through a float first
        would round the money twice on the way to a number we then round again.
        A float is still accepted (the backend-reported cost arrives as one) and
        is converted via ``str`` so it takes its printed value rather than its
        binary one.

        Banker's rounding, unchanged: ``ROUND_HALF_EVEN`` is what ``round()`` was
        already doing, so no existing bill moves by a credit. A non-positive cost
        yields 0 credits (no debit; the run is still marked billed so it isn't
        re-swept).
        """
        if cost_usd <= 0:
            return 0
        exact = cost_usd if isinstance(cost_usd, Decimal) else Decimal(str(cost_usd))
        credits = exact * Decimal(str(self.markup)) / Decimal(str(self.credit_usd))
        return int(credits.quantize(Decimal("1"), rounding=ROUND_HALF_EVEN))


@dataclass(frozen=True)
class ComputeCost:
    """A run's resolved compute cost (the Meter primitive's output).

    ``cost_usd`` is the authoritative USD cost as a float, which is the shape
    that gets serialised onto the ledger ref and ``BillResult``. ``source``
    records how it was resolved (the backend's reported ``total_cost_usd``, the
    dated price lookup, ``unpriced`` for a real run nothing could price, or
    ``none`` for empty usage). ``model`` is the model the cost was attributed to
    (``None`` when usage carried no model).

    ``cost_usd_exact`` is the SAME number before it met a float, and it is what
    ``RateCard.to_credits`` should be handed. ``None`` when the cost never was a
    Decimal (an unpriced or empty run), in which case there is nothing to bill
    anyway.
    """

    cost_usd: float
    source: CostSource
    model: str | None
    cost_usd_exact: Decimal | None = None
