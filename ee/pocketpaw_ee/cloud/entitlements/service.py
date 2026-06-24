# ee/pocketpaw_ee/cloud/entitlements/service.py — the entitlements RESOLVER
# (BC-6, the Entitlement primitive).
#
# Module-level ``async def`` API (NOT a class, per EE cloud rule, mirroring
# ``credits.service`` / ``billing.service``). Public API:
#   * ``resolve_entitlements(workspace_id)`` — read the workspace's CURRENT plan
#     (``workspace.service.get_workspace_plan``), look it up in the billing plan
#     catalog (``billing.plans``), and return an ``Entitlements`` (plan +
#     features + monthly credit allotment).
#
# READ-ONLY: no writes, no emit (EE cloud rule 9 only fires on mutation; this
# entity mutates nothing). Tenancy: ``get_workspace_plan`` resolves the plan from
# the ONE ``Workspace`` document with that id and returns None for a missing /
# soft-deleted / malformed id, so there is no cross-tenant read here — a caller
# only ever sees the plan of the workspace it asked for.
#
# FALLBACK: a workspace with no plan, an unknown plan string, or a missing
# workspace resolves to the ``free`` base tier (``plans.BASE_PLAN_KEY``) — never
# a crash, never a silent upgrade to a paid tier. (Subscription EVENTS that
# CHANGE the plan are BC-7's job; here entitlements derive from the existing
# ``Workspace.plan`` field as it stands.)
#
# Created 2026-06-24 (integration/billing-credits, BC-6): new entity.

from __future__ import annotations

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.billing import plans as plan_catalog
from pocketpaw_ee.cloud.entitlements.domain import Entitlements


async def resolve_entitlements(workspace_id: str) -> Entitlements:
    """Resolve a workspace to its entitlements (plan + features + allotment).

    Reads the workspace's CURRENT ``Workspace.plan`` and looks the tier up in the
    billing plan catalog. A workspace with no/unknown plan (or one that doesn't
    exist) resolves to the ``free`` base tier — never a crash, never a paid-tier
    leak.
    """
    # Rule 6 — validate at entry.
    if not workspace_id:
        raise ValidationError("entitlements.invalid_workspace", "workspace_id is required")

    # Lazy import keeps this module free of the heavy workspace.service import at
    # module load (it pulls Beanie), mirroring how the plan-feature gate dep
    # imports the workspace service inside the guard.
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    plan_key = await workspace_service.get_workspace_plan(workspace_id)

    # None (missing/deleted/malformed id) OR a plan string not in the catalog
    # (a stale/typo'd tier) both fall back to the base floor. ``get_plan``
    # returns None for an unknown key, so this one branch covers both.
    tier = plan_catalog.get_plan(plan_key)
    if tier is None:
        tier = plan_catalog.get_plan(plan_catalog.BASE_PLAN_KEY)
        # The base tier is a static catalog entry; this is never None in
        # practice, but guard so a future catalog edit can't NPE the resolver.
        if tier is None:  # pragma: no cover - defensive; base tier always exists
            return Entitlements(
                workspace_id=workspace_id,
                plan=plan_catalog.BASE_PLAN_KEY,
                monthly_credit_allotment=0,
                features=frozenset(),
            )

    return Entitlements(
        workspace_id=workspace_id,
        plan=tier.key,
        monthly_credit_allotment=tier.monthly_credit_allotment,
        features=tier.features,
    )
