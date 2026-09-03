# ee/pocketpaw_ee/cloud/metering/dto.py — result schema for the compute-cost
# metering entity (BC-3, the Meter + Price primitives).
#
# Metering is an internal system job (the sweeper drives it; there is no public
# CRUD route in BC-3), so the only DTO is the BILL RESULT — what ``bill_run``
# returns to the sweeper / a caller so it can log or assert on the outcome
# without reaching back into the Beanie doc. Distinct from the domain
# ``ComputeCost`` (the meter's intermediate output): this is the post-debit
# outcome (credits charged, the resulting balance, whether a debit actually ran).
#
# Created 2026-06-24 (integration/billing-credits, BC-3): new entity.

from __future__ import annotations

from pydantic import BaseModel, Field


class BillResult(BaseModel):
    """The outcome of billing one run's compute cost.

    ``credits_charged`` is the WHOLE credits debited, truncated for display, so a
    run costing 3.125 credits reports 3. ``micro_charged`` is the exact figure
    (1_000_000 == 1 credit) and is what the wallet actually moved — read that one
    to reconcile against the ledger. A cheap run can charge real money and still
    show ``credits_charged == 0``; only ``debited`` says whether money moved.
    ``balance_after`` is the workspace wallet balance once the debit landed (may
    be negative: a completed run is always billed, so an overage is recorded as a
    legitimate negative). ``debited`` is False only when the run was genuinely free
    and no ledger movement was written.
    """

    run_id: str
    workspace_id: str
    cost_usd: float
    credits_charged: int
    micro_charged: int = 0
    balance_after: int
    debited: bool
    cost_source: str = Field(
        description="How the cost was resolved: 'reported' | 'estimated' | 'none'.",
    )
    model: str | None = None
