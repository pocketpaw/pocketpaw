# ee/pocketpaw_ee/cloud/entitlements/domain.py — the frozen, framework-free
# value object the entitlements resolver returns (BC-6, the Entitlement
# primitive).
#
# ``Entitlements`` is the normalized "what is this workspace entitled to" shape:
# its resolved plan key, the plan's feature set, and its monthly credit
# allotment. It carries no framework type (no Beanie, no FastAPI) so the service
# can build it and the DTO layer can map it without either reaching into the
# other. It is derived from the EXISTING ``Workspace.plan`` field + the billing
# plan catalog — there is no event projection here.
#
# Created 2026-06-24 (integration/billing-credits, BC-6): new entity.

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
    per renewal for the tier.
    """

    workspace_id: str
    plan: str
    monthly_credit_allotment: int
    features: frozenset[str] = field(default_factory=frozenset)
