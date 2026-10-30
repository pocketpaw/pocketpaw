# ee/pocketpaw_ee/cloud/partners/_calc.py — the partner money rules, pure.
#
# Created 2026-10-02 (feat/partners-commissions, PH-13): ONE function decides the
# rate a client payment earns its partner.
# Updated 2026-10-02 (feat/partners-tiers, PH-15): VOLUME TIERS and MILESTONE
# REWARDS. Every threshold, percentage and reward lives in the two tables below
# (``TIERS``, ``MILESTONES``) and nowhere else; change a number here and the
# offers, the sale/renewal charge, the commission and the reward ladder all follow.
#
#   * Tier from ACTIVE sold sites: bronze 0-9, silver 10-24, gold 25+.
#   * Wholesale discount on the partner price: 0% / 10% / 20%, the partner pays
#     floor(price x (1 - d)) in whole USD (``discounted_usd``).
#   * Commission on a client payment: 25% / 30% / 35%. A FOUNDING partner earns
#     max(40%, tier rate) within 24 months of that site's first client payment,
#     then the tier rate.
#   * Milestones on LIFETIME distinct sites sold: 1st +200, 10th +1,000,
#     25th +3,000, 50th +7,500 credits, once each.
#
# An unknown tier name reads as bronze: no discount, the lowest commission (the
# conservative side of every rule). Rates are basis points so all arithmetic is
# integer (floor) with no float rounding.

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from dateutil.relativedelta import relativedelta


class Tier(NamedTuple):
    name: str
    at: int  # active sold sites needed
    discount_bps: int  # off the wholesale partner price
    commission_bps: int  # of a client payment


# Ascending by ``at``; the first row is the floor and must start at 0.
TIERS: tuple[Tier, ...] = (
    Tier("bronze", 0, 0, 2_500),
    Tier("silver", 10, 1_000, 3_000),
    Tier("gold", 25, 2_000, 3_500),
)
# (lifetime distinct sites sold, credits granted once)
MILESTONES: tuple[tuple[int, int], ...] = ((1, 200), (10, 1_000), (25, 3_000), (50, 7_500))

BASE_RATE_BPS = TIERS[0].commission_bps
FOUNDING_RATE_BPS = 4_000
FOUNDING_WINDOW_MONTHS = 24

_BY_NAME = {t.name: t for t in TIERS}


def _tier(name: str) -> Tier:
    return _BY_NAME.get(name, TIERS[0])


def tier_for(active_sites: int) -> str:
    """The tier ``active_sites`` active sold sites earn."""
    return [t for t in TIERS if max(active_sites, 0) >= t.at][-1].name


def rank(name: str) -> int:
    """0 for bronze upward; an unknown name ranks as bronze."""
    return TIERS.index(_tier(name))


def next_tier(active_sites: int) -> dict | None:
    """``{name, at, remaining}`` for the next tier up, or None at the top."""
    up = next((t for t in TIERS if t.at > active_sites), None)
    if up is None:
        return None
    return {"name": up.name, "at": up.at, "remaining": up.at - max(active_sites, 0)}


def wholesale_discount_bps(tier: str) -> int:
    return _tier(tier).discount_bps


def tier_commission_bps(tier: str) -> int:
    return _tier(tier).commission_bps


def discounted_usd(price_usd: int, tier: str) -> int:
    """Whole USD the partner pays: floor(price x (1 - discount))."""
    return int(price_usd) * (10_000 - wholesale_discount_bps(tier)) // 10_000


def commission_rate_bps(
    *,
    founding: bool,
    first_client_payment_at: datetime,
    paid_at: datetime,
    tier: str,
) -> int:
    """Basis points of the paid amount this payment earns the partner."""
    rate = tier_commission_bps(tier)
    if founding and paid_at < first_client_payment_at + relativedelta(
        months=FOUNDING_WINDOW_MONTHS
    ):
        return max(FOUNDING_RATE_BPS, rate)
    return rate


def commission_credits(paid_usd_cents: int, rate_bps: int) -> int:
    """Credits (1 credit = 1 US cent) for ``paid_usd_cents`` at ``rate_bps``, floored."""
    return max(int(paid_usd_cents), 0) * int(rate_bps) // 10_000


def milestones_reached(lifetime_sold: int) -> list[int]:
    """The milestone site counts ``lifetime_sold`` has reached, ascending."""
    return [sites for sites, _ in MILESTONES if lifetime_sold >= sites]
