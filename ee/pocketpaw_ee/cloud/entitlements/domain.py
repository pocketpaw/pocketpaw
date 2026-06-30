# ee/pocketpaw_ee/cloud/entitlements/domain.py — the frozen, framework-free
# value object the entitlements resolver returns (BC-6, the Entitlement
# primitive).
#
# ``Entitlements`` is the normalized "what is this workspace entitled to" shape:
# its resolved plan key, the plan's feature set, its monthly credit allotment, and
# its monthly credit ceiling (the quota cap). It carries no framework type (no
# Beanie, no FastAPI) so the service can build it and the DTO layer can map it
# without either reaching into the other. It is derived from the EXISTING
# ``Workspace.plan`` field + the billing plan catalog — there is no event
# projection here.
#
# Created 2026-06-24 (integration/billing-credits, BC-6): new entity.
# Updated 2026-06-30 (feat/billing-quota-enforcement, chunk 1): added
#   ``monthly_ceiling: int | None`` next to ``monthly_credit_allotment`` — the
#   per-plan monthly credit CAP (None = uncapped) the resolver populates from the
#   plan catalog and later quota chunks enforce against.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Entitlements:
    """What a workspace is entitled to, resolved from its current plan.

    ``plan`` is the resolved tier key (matches ``Workspace.plan`` /
    ``PLAN_FEATURES`` — a workspace with no/unknown plan resolves to the base
    ``free`` tier). ``features`` is that tier's feature set (the same set
    ``PLAN_FEATURES`` and the policy gate use — one source of truth).
    ``monthly_credit_allotment`` is integer credits (1 credit == $0.01) granted
    per renewal for the tier. ``monthly_ceiling`` is the per-plan monthly credit
    CAP (integer credits, or None = uncapped) credit-quota enforcement caps spend
    against; a workspace with no/unknown plan resolves to the Free ceiling (the
    fail-closed trial cap), never None/uncapped.
    """

    workspace_id: str
    plan: str
    monthly_credit_allotment: int
    monthly_ceiling: int | None
    features: frozenset[str] = field(default_factory=frozenset)
