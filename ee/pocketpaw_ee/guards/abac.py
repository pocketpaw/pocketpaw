# Attribute-based policy rules — plan gates, action-role mapping, tool whitelist.
# Created: 2026-04-10
# Updated: 2026-06-24 (integration/billing-credits, BC-6) — added a minimal
#   ``free`` base tier to PLAN_FEATURES so the billing catalog and the
#   entitlements resolver have an explicit floor to fall back to (a workspace
#   with no/unknown plan resolves to ``free``). PLAN_FEATURES stays the single
#   source of truth for which features a tier has; the billing catalog
#   (``ee.cloud.billing.plans``) references it rather than duplicating it.
# Updated 2026-06-25 (feat/consumer-plan-ladder) — rekeyed PLAN_FEATURES from the
#   old {free, team, business, enterprise} tiers to the approved CONSUMER ladder
#   {free, go, pro, pro_max, enterprise}. Mapping: team->go, business->pro, plus a
#   new pro_max tier between pro and enterprise.
# Updated 2026-06-25 (decouple-sites-from-fabric) — the ``fabric`` flag was
#   OVERLOADED: it gated Sites, Leads, AND the enterprise-only Fabric ontology.
#   The consumer ladder gives Paw Go a site, so Sites + Leads now gate on a NEW
#   ENFORCED ``sites`` flag (present go onward), and ``fabric`` is kept for the
#   Fabric ontology ONLY — now enterprise-ONLY. Consequence: the ladder is NO
#   LONGER a strict superset on ``fabric`` — go/pro/pro_max carry ``sites`` but NOT
#   ``fabric``; only enterprise carries ``fabric``. Intentional and correct (the
#   ontology is an enterprise capability, not a step on the consumer ladder).
#   Other display-only flags (studio, code, deep_work, chain_flow, fleet, belt,
#   foresight) are surfaced to the UI but not yet enforced. ``automations`` stays
#   on pro+; ``audit``/``sso``/``instinct``/``custom_roles`` stay enterprise-only.

from __future__ import annotations

import logging

from pocketpaw_ee.guards.policy import PolicyContext, PolicyResult
from pocketpaw_ee.guards.rbac import WorkspaceRole

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plan feature gates
# ---------------------------------------------------------------------------

# The CONSUMER plan ladder. The paid tiers nest on capability EXCEPT the
# ``fabric`` flag, which is enterprise-only (see below) — so go/pro/pro_max are a
# superset chain among themselves, and enterprise adds the enterprise-only gates
# on top. Two kinds of feature string live here:
#   * ENFORCED flags — checked at a call site (the ABAC gate's ``_feature_for_action``,
#     ``require_plan_feature``, the Sites/Leads ``sites`` gate). Keep them on the
#     tiers that should have them: ``sites`` (Sites + Leads) on go+, ``automations``
#     on pro+, ``fabric`` (the Fabric ONTOLOGY only) enterprise-only, and
#     ``audit``/``sso``/``instinct``/``custom_roles`` enterprise-only.
#   * DISPLAY-ONLY flags — surfaced to the billing UI to describe what a tier
#     unlocks, not yet wired to any gate (studio, code, deep_work, chain_flow,
#     fleet, belt, foresight). Adding one is safe: nothing enforces it.
PLAN_FEATURES: dict[str, set[str]] = {
    # The base/free floor — a workspace with no paid plan (or an unknown plan)
    # resolves here. Deliberately minimal: the core canvas (pockets) and the
    # ability to run sessions, nothing that costs the platform a paid upstream.
    "free": {"pockets", "sessions"},
    # Paw Go — everyday: chat, pockets, agents, memory + Studio + Sites. ``sites``
    # is ENFORCED here (go gets a site); ``fabric`` (the ontology) is NOT.
    "go": {
        "pockets",
        "sessions",
        "agents",
        "memory",
        "studio",
        "sites",
    },
    # Paw Pro — daily drivers. Adds automations + knowledge_base + display flags
    # for the power surfaces (code, deep_work, chain_flow, fleet). Keeps ``sites``;
    # does NOT carry ``fabric`` (the ontology is enterprise-only).
    "pro": {
        "pockets",
        "sessions",
        "agents",
        "memory",
        "studio",
        "sites",
        "automations",
        "knowledge_base",
        "code",
        "deep_work",
        "chain_flow",
        "fleet",
    },
    # Paw Pro Max — uncapped power users. Everything in pro + belt + foresight.
    # Still NO ``fabric`` (enterprise-only ontology).
    "pro_max": {
        "pockets",
        "sessions",
        "agents",
        "memory",
        "studio",
        "sites",
        "automations",
        "knowledge_base",
        "code",
        "deep_work",
        "chain_flow",
        "fleet",
        "belt",
        "foresight",
    },
    # Enterprise — the full set: every consumer flag PLUS the enterprise-only
    # gates (the ``fabric`` ONTOLOGY, instinct, audit, sso, custom_roles).
    "enterprise": {
        "pockets",
        "sessions",
        "agents",
        "memory",
        "studio",
        "sites",
        "automations",
        "knowledge_base",
        "code",
        "deep_work",
        "chain_flow",
        "fleet",
        "belt",
        "foresight",
        "fabric",
        "instinct",
        "audit",
        "sso",
        "custom_roles",
    },
}


# ---------------------------------------------------------------------------
# Action -> minimum role mapping
# ---------------------------------------------------------------------------

ACTION_ROLES: dict[str, WorkspaceRole] = {
    "workspace.update": WorkspaceRole.ADMIN,
    "workspace.delete": WorkspaceRole.OWNER,
    "workspace.invite": WorkspaceRole.ADMIN,
    "member.remove": WorkspaceRole.ADMIN,
    "member.role_change": WorkspaceRole.OWNER,
    "pocket.create": WorkspaceRole.MEMBER,
    "pocket.delete": WorkspaceRole.ADMIN,
    "agent.create": WorkspaceRole.ADMIN,
    "agent.run": WorkspaceRole.MEMBER,
    "agent.delete": WorkspaceRole.ADMIN,
    "automation.create": WorkspaceRole.ADMIN,
    "automation.run": WorkspaceRole.MEMBER,
    "settings.read": WorkspaceRole.MEMBER,
    "settings.write": WorkspaceRole.ADMIN,
    "audit.read": WorkspaceRole.ADMIN,
    "billing.manage": WorkspaceRole.OWNER,
}


# ---------------------------------------------------------------------------
# Agent tool whitelist per workspace role
# ---------------------------------------------------------------------------

ROLE_TOOL_LIMITS: dict[WorkspaceRole, set[str] | None] = {
    WorkspaceRole.MEMBER: {
        "web_search",
        "research",
        "memory",
        "soul_recall",
        "soul_remember",
    },
    WorkspaceRole.ADMIN: None,
    WorkspaceRole.OWNER: None,
}


# ---------------------------------------------------------------------------
# Policy evaluation
# ---------------------------------------------------------------------------


def _feature_for_action(action: str) -> str | None:
    """Derive the plan feature name from an action prefix."""
    prefix = action.split(".")[0] if "." in action else action
    # Map action prefixes to plan feature names
    mapping = {
        "automation": "automations",
        "audit": "audit",
        "sso": "sso",
        "fabric": "fabric",
        "instinct": "instinct",
    }
    return mapping.get(prefix)


def evaluate_policy(ctx: PolicyContext) -> PolicyResult:
    """Evaluate all ABAC rules against a context. Returns first denial or allow."""

    # Check 1: Plan feature gate
    feature = _feature_for_action(ctx.action)
    if feature is not None:
        allowed_features = PLAN_FEATURES.get(ctx.plan, set())
        if feature not in allowed_features:
            return PolicyResult(
                allowed=False,
                code="plan.feature_denied",
                detail=f"Feature {feature!r} requires a higher plan (current: {ctx.plan})",
            )

    # Check 2: Role minimum for action
    minimum_role = ACTION_ROLES.get(ctx.action)
    if minimum_role is not None and ctx.role.level < minimum_role.level:
        return PolicyResult(
            allowed=False,
            code="workspace.insufficient_role",
            detail=f"Action {ctx.action!r} requires {minimum_role.value}, got {ctx.role.value}",
        )

    # Check 3: Agent permission ceiling — agent can't exceed creator's role
    if ctx.agent_id is not None and ctx.agent_creator_role is not None:
        if ctx.role.level > ctx.agent_creator_role.level:
            return PolicyResult(
                allowed=False,
                code="agent.ceiling_exceeded",
                detail=f"Agent {ctx.agent_id} was created by {ctx.agent_creator_role.value}, "
                f"cannot act as {ctx.role.value}",
            )

    # Check 4: Tool whitelist (if action is tool-scoped)
    if ctx.action.startswith("tool."):
        tool_name = ctx.action.removeprefix("tool.")
        allowed_tools = ROLE_TOOL_LIMITS.get(ctx.role)
        if allowed_tools is not None and tool_name not in allowed_tools:
            return PolicyResult(
                allowed=False,
                code="agent.tool_not_allowed",
                detail=f"Role {ctx.role.value} cannot use tool {tool_name!r}",
            )

    return PolicyResult(allowed=True)
