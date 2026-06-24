# ee/pocketpaw_ee/cloud/billing/plans.py — the PLAN CATALOG: the billing/
# declarative view of the workspace plan tiers (BC-6, the Plan primitive).
#
# This is the read-only catalog the subscription (BC-7) and per-site (BC-9)
# layers build on. For each EXISTING tier in ``guards.abac.PLAN_FEATURES`` it
# pairs the tier's feature set with its billing facts:
#   * ``monthly_credit_allotment`` — credits granted per renewal (integer credits,
#     1 credit == $0.01). Config-tunable constants live below; BC-7 will grant
#     these on a subscription renewal.
#   * ``dodo_product_id`` — the Dodo recurring-product id for the tier, or None
#     until BC-7 / config populates it (optionally read from the
#     ``POCKETPAW_DODO_PLAN_PRODUCTS`` mapping setting when configured).
#   * ``features`` — SOURCED FROM ``PLAN_FEATURES`` (referenced, never duplicated)
#     so there is exactly one source of truth for what a tier unlocks.
#
# Read-only by design: no DB, no writes, no emit. ``list_plans`` / ``get_plan``
# return frozen ``PlanTier`` value objects built fresh from the live
# ``PLAN_FEATURES`` mapping + the allotment constants, so the catalog can never
# drift from the policy gate.
#
# Created 2026-06-24 (integration/billing-credits, BC-6): new module.

from __future__ import annotations

from dataclasses import dataclass

from pocketpaw_ee.guards.abac import PLAN_FEATURES

# ---------------------------------------------------------------------------
# Tunable constants — the monthly credit allotment per tier.
# ---------------------------------------------------------------------------
#
# Integer credits, 1 credit == $0.01 (the same denomination the ledger and Dodo
# top-ups use). These are the per-renewal grant amounts BC-7 will apply on a
# subscription cycle. Defaults are deliberately round and modest — a real price
# sheet tunes them, but the SHAPE (free=0, then a growing ladder) is the
# contract. Picked so the ladder is obvious: free grants nothing, each paid tier
# is a clear step up.
#
#   free        =      0 credits  ($0)      — no recurring grant
#   team        = 50_000 credits  ($500)
#   business    = 200_000 credits ($2,000)
#   enterprise  = 1_000_000 credits ($10,000)
_MONTHLY_CREDIT_ALLOTMENT: dict[str, int] = {
    "free": 0,
    "team": 50_000,
    "business": 200_000,
    "enterprise": 1_000_000,
}

# Order the catalog is listed in — the price ladder, cheapest first. Any tier in
# PLAN_FEATURES not named here is appended afterwards (so a new tier never
# silently drops out of the catalog).
_TIER_ORDER: tuple[str, ...] = ("free", "team", "business", "enterprise")


@dataclass(frozen=True)
class PlanTier:
    """One row of the billing plan catalog — the declarative view of a tier.

    ``key`` matches the ``Workspace.plan`` string and the ``PLAN_FEATURES`` key.
    ``features`` is a copy of the tier's ``PLAN_FEATURES`` set (the source of
    truth), so a caller can read it without reaching into the policy module.
    ``monthly_credit_allotment`` is integer credits (1 credit == $0.01) granted
    per renewal. ``dodo_product_id`` is the recurring-product id, or None until
    BC-7 / config populates it.
    """

    key: str
    monthly_credit_allotment: int
    dodo_product_id: str | None
    features: frozenset[str]


def _dodo_product_for(key: str) -> str | None:
    """Resolve the Dodo recurring-product id for a tier, or None.

    Reads an optional ``POCKETPAW_DODO_PLAN_PRODUCTS`` mapping
    (``{tier_key: product_id}``) off settings when present. None is the correct
    default in v1 — BC-7 wires recurring checkout and populates this. Lazy
    ``get_settings`` import so building the catalog never forces config load in
    a context that doesn't have it (e.g. a unit test of ``list_plans``); any
    config error degrades safely to None rather than breaking the read.
    """
    try:
        from pocketpaw.config import get_settings

        mapping = getattr(get_settings(), "dodo_plan_products", None)
    except Exception:
        return None
    if not isinstance(mapping, dict):
        return None
    val = mapping.get(key)
    return val if isinstance(val, str) and val else None


def _build(key: str) -> PlanTier:
    """Construct a ``PlanTier`` for ``key`` from the live PLAN_FEATURES + config.

    ``features`` is copied from PLAN_FEATURES (referenced, not duplicated) so the
    catalog can never drift from the policy gate. An unknown ``key`` yields an
    empty feature set and a 0 allotment — but callers go through ``get_plan`` /
    ``list_plans``, which only ever pass known keys.
    """
    return PlanTier(
        key=key,
        monthly_credit_allotment=_MONTHLY_CREDIT_ALLOTMENT.get(key, 0),
        dodo_product_id=_dodo_product_for(key),
        features=frozenset(PLAN_FEATURES.get(key, set())),
    )


def _catalog_keys() -> list[str]:
    """All tier keys, in ladder order, with any unlisted tiers appended.

    Driven off the LIVE ``PLAN_FEATURES`` keys so a new tier added to the policy
    module shows up in the catalog automatically (appended after the ordered
    ones) instead of being silently dropped.
    """
    known = list(PLAN_FEATURES.keys())
    ordered = [k for k in _TIER_ORDER if k in known]
    extra = [k for k in known if k not in _TIER_ORDER]
    return ordered + extra


def list_plans() -> list[PlanTier]:
    """Return the full plan catalog, cheapest tier first.

    Each ``PlanTier`` is built fresh from the live ``PLAN_FEATURES`` mapping +
    the allotment constants, so the catalog always matches the policy gate.
    """
    return [_build(key) for key in _catalog_keys()]


def get_plan(key: str | None) -> PlanTier | None:
    """Resolve a single tier by its ``key`` (the ``Workspace.plan`` string).

    Returns None for an unknown / missing key. Callers that need a guaranteed
    floor (the entitlements resolver) map a None back to the ``free`` base tier
    themselves — this function does NOT silently substitute, so a typo in a
    lookup is visible rather than masked.
    """
    if not key or key not in PLAN_FEATURES:
        return None
    return _build(key)


# The base/floor tier key — a workspace with no/unknown plan resolves here.
BASE_PLAN_KEY = "free"
