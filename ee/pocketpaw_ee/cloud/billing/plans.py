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
# Updated 2026-06-25 (feat/consumer-plan-ladder) — rekeyed the catalog to the
#   approved CONSUMER ladder {free, go, pro, pro_max, enterprise} (was {free, team,
#   business, enterprise}); reset ``_MONTHLY_CREDIT_ALLOTMENT`` + ``_TIER_ORDER``
#   to the new keys; ADDED user-facing display + price metadata (``_PLAN_DISPLAY``)
#   so the billing UI can show ChatGPT/Claude-style "usage" wording (display_name,
#   usage_label, usage_detail) and INR/USD monthly+annual prices instead of raw
#   credits. ``monthly_credit_allotment`` stays in the catalog as a back-office
#   field; it is NOT the headline the UI shows. Enterprise prices are None ("talk
#   to us").

from __future__ import annotations

from dataclasses import dataclass

from pocketpaw_ee.guards.abac import PLAN_FEATURES

# ---------------------------------------------------------------------------
# Tunable constants — the monthly credit allotment per tier.
# ---------------------------------------------------------------------------
#
# Integer credits, 1 credit == $0.01 (the same denomination the ledger and Dodo
# top-ups use). These are the per-renewal grant amounts BC-7 applies on a
# subscription cycle. The SHAPE (free=0, then a growing ladder) is the contract:
# free grants nothing, each paid tier is a clear step up. These are a BACK-OFFICE
# field — the UI headlines the ``usage_label`` from ``_PLAN_DISPLAY`` instead.
#
#   free        =         0 credits  — no recurring grant
#   go          =     1_500 credits  — everyday usage
#   pro         =     7_500 credits  — ~5x the everyday usage
#   pro_max     =    30_000 credits  — ~20x the usage
#   enterprise  = 1_000_000 credits  — custom / effectively uncapped
_MONTHLY_CREDIT_ALLOTMENT: dict[str, int] = {
    "free": 0,
    "go": 1_500,
    "pro": 7_500,
    "pro_max": 30_000,
    "enterprise": 1_000_000,
}

# Order the catalog is listed in — the price ladder, cheapest first. Any tier in
# PLAN_FEATURES not named here is appended afterwards (so a new tier never
# silently drops out of the catalog).
_TIER_ORDER: tuple[str, ...] = ("free", "go", "pro", "pro_max", "enterprise")

# ---------------------------------------------------------------------------
# User-facing DISPLAY + PRICE metadata.
# ---------------------------------------------------------------------------
#
# What the billing UI actually shows. ``usage_label`` is the ChatGPT/Claude-style
# headline ("Everyday", "5x the usage", ...) the UI renders INSTEAD of raw
# credits; ``usage_detail`` is one supporting line. Prices are integers in the
# tier's natural denomination (INR rupees, USD dollars), monthly + annual.
# ``free`` is all-zero; ``enterprise`` prices are None ("talk to us").
_PLAN_DISPLAY: dict[str, dict[str, object]] = {
    "free": {
        "display_name": "Free",
        "usage_label": "Limited",
        "usage_detail": "Chat + pockets to get started",
        "price_inr_monthly": 0,
        "price_inr_annual": 0,
        "price_usd_monthly": 0,
        "price_usd_annual": 0,
    },
    "go": {
        "display_name": "Paw Go",
        "usage_label": "Everyday",
        "usage_detail": "Everyday agent + site usage",
        "price_inr_monthly": 399,
        "price_inr_annual": 3990,
        "price_usd_monthly": 9,
        "price_usd_annual": 90,
    },
    "pro": {
        "display_name": "Paw Pro",
        "usage_label": "5× the usage",
        "usage_detail": "5× the everyday usage — for daily drivers",
        "price_inr_monthly": 1499,
        "price_inr_annual": 14990,
        "price_usd_monthly": 19,
        "price_usd_annual": 190,
    },
    "pro_max": {
        "display_name": "Paw Pro Max",
        "usage_label": "20× the usage",
        "usage_detail": "20× the usage — uncapped power users",
        "price_inr_monthly": 4999,
        "price_inr_annual": 49990,
        "price_usd_monthly": 49,
        "price_usd_annual": 490,
    },
    "enterprise": {
        "display_name": "Enterprise",
        "usage_label": "Custom",
        "usage_detail": "Custom usage, SSO, audit — talk to us",
        "price_inr_monthly": None,
        "price_inr_annual": None,
        "price_usd_monthly": None,
        "price_usd_annual": None,
    },
}

# A safe display default for a tier that has no ``_PLAN_DISPLAY`` row (a tier
# added to PLAN_FEATURES but not yet given UI copy). Keeps the catalog readable
# rather than NPE-ing on a missing key.
_DISPLAY_FALLBACK: dict[str, object] = {
    "display_name": "",
    "usage_label": "",
    "usage_detail": "",
    "price_inr_monthly": None,
    "price_inr_annual": None,
    "price_usd_monthly": None,
    "price_usd_annual": None,
}


@dataclass(frozen=True)
class PlanTier:
    """One row of the billing plan catalog — the declarative view of a tier.

    ``key`` matches the ``Workspace.plan`` string and the ``PLAN_FEATURES`` key.
    ``features`` is a copy of the tier's ``PLAN_FEATURES`` set (the source of
    truth), so a caller can read it without reaching into the policy module.
    ``monthly_credit_allotment`` is integer credits (1 credit == $0.01) granted
    per renewal — a BACK-OFFICE field, NOT the headline. ``dodo_product_id`` is
    the recurring-product id, or None until BC-7 / config populates it.

    The display + price fields are what the billing UI renders: ``display_name``
    ("Paw Pro"), ``usage_label`` (the ChatGPT/Claude-style "5x the usage" headline
    shown instead of credits), ``usage_detail`` (one supporting line), and the
    INR/USD monthly+annual prices (integers; ``free`` = 0; ``enterprise`` = None
    ⇒ "talk to us").
    """

    key: str
    monthly_credit_allotment: int
    dodo_product_id: str | None
    features: frozenset[str]
    display_name: str
    usage_label: str
    usage_detail: str
    price_inr_monthly: int | None
    price_inr_annual: int | None
    price_usd_monthly: int | None
    price_usd_annual: int | None


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
    catalog can never drift from the policy gate. The display + price fields come
    from ``_PLAN_DISPLAY`` (a missing row degrades to ``_DISPLAY_FALLBACK`` rather
    than NPE-ing). An unknown ``key`` yields an empty feature set and a 0
    allotment — but callers go through ``get_plan`` / ``list_plans``, which only
    ever pass known keys.
    """
    display = _PLAN_DISPLAY.get(key, _DISPLAY_FALLBACK)
    return PlanTier(
        key=key,
        monthly_credit_allotment=_MONTHLY_CREDIT_ALLOTMENT.get(key, 0),
        dodo_product_id=_dodo_product_for(key),
        features=frozenset(PLAN_FEATURES.get(key, set())),
        display_name=str(display["display_name"]),
        usage_label=str(display["usage_label"]),
        usage_detail=str(display["usage_detail"]),
        price_inr_monthly=display["price_inr_monthly"],  # type: ignore[arg-type]
        price_inr_annual=display["price_inr_annual"],  # type: ignore[arg-type]
        price_usd_monthly=display["price_usd_monthly"],  # type: ignore[arg-type]
        price_usd_annual=display["price_usd_annual"],  # type: ignore[arg-type]
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
