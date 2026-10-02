# ee/pocketpaw_ee/cloud/partners/_calc.py — the partner commission rule, pure.
#
# Created 2026-10-02 (feat/partners-commissions, PH-13). ONE function decides the
# rate a client payment earns its partner, so volume-based partner tiers (PH-15)
# are a change here and nowhere else. ``tier`` is accepted and unused today.
#
# Rule: 25% of the paid amount; a FOUNDING partner earns 40% on payments made
# within 24 months of that site's first client payment, then 25%. Rates are basis
# points so the commission is integer arithmetic (floor) with no float rounding.

from __future__ import annotations

from datetime import datetime

from dateutil.relativedelta import relativedelta

BASE_RATE_BPS = 2_500
FOUNDING_RATE_BPS = 4_000
FOUNDING_WINDOW_MONTHS = 24


def commission_rate_bps(
    *,
    founding: bool,
    first_client_payment_at: datetime,
    paid_at: datetime,
    tier: str,
) -> int:
    """Basis points of the paid amount this payment earns the partner."""
    del tier  # PH-15: volume tiers will read it.
    if founding and paid_at < first_client_payment_at + relativedelta(
        months=FOUNDING_WINDOW_MONTHS
    ):
        return FOUNDING_RATE_BPS
    return BASE_RATE_BPS


def commission_credits(paid_usd_cents: int, rate_bps: int) -> int:
    """Credits (1 credit = 1 US cent) for ``paid_usd_cents`` at ``rate_bps``, floored."""
    return max(int(paid_usd_cents), 0) * int(rate_bps) // 10_000
