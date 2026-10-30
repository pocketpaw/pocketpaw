# Attribute-based policy rules — plan gates, action-role mapping, tool whitelist.
# Created: 2026-04-10
# Updated: 2026-08-08 (feat/billing-rbac-member-caps) — the Belt & Foresight
#   display flags moved DOWN to Paw Pro (was Pro Max only), matching the billing
#   UI decision that both Pro tiers unlock them. Still display-only (not yet
#   enforced); Pro Max remains differentiated by usage/price, not features.
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
# Updated 2026-09-01 (fix/instinct-is-a-gate-not-a-tier) — ``instinct`` moved OUT
#   of the enterprise-only set and onto EVERY tier, free included. It is not a
#   capability you buy; it is the APPROVAL GATE agents propose through, and the
#   gate it was priced above was HALF-OPEN. Roughly twenty modules WRITE Instinct
#   actions (belt, external actions, growth, fabric conflicts/proposals, admin
#   proposals, the site-publish merge gate, the chat agent service) and almost
#   none of them check this flag. Exactly ONE place reads and approves them —
#   ``instinct/router.py``'s ``require_plan_feature("instinct")`` — so on
#   free/go/pro/pro_max those proposals were created and could never be read,
#   approved, or rejected. Not "unavailable": a queue with no door. The reported
#   symptom was a workspace OWNER unable to publish a site, because the builder's
#   Publish button self-approves through ``instinct/actions/pending`` +
#   ``/approve`` and both 403'd with ``plan.feature_denied``.
#   The floor gets it for the same reason the rest do: ``free`` carries
#   ``sessions``, so agents ACT there, and oversight of agent actions cannot be
#   the thing a cheaper tier does without. It costs no paid upstream either —
#   the store is a local per-workspace SQLite file — which is the stated bar for
#   the free floor.
#   THIS LOOSENS NO PERMISSION. ``instinct.approve`` is still ADMIN in
#   ``guards/actions.py``, the artifact-change workspace tenancy asserts still
#   run, and ``require_license`` still gates the router. What is removed is a
#   BILLING gate that sat redundantly on top of a working PERMISSION gate.
#   What stays enterprise is the intelligence built ON Instinct — ``audit``
#   (the ledger + its export) and ``custom_roles`` — which are keyed separately
#   and untouched here.

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
    # ``instinct`` is here because the floor RUNS AGENTS (``sessions``), and the
    # approval gate has to exist wherever agents act — see the note above.
    "free": {"pockets", "sessions", "instinct"},
    # Paw Go — everyday: chat, pockets, agents, memory + Studio + Sites. ``sites``
    # is ENFORCED here (go gets a site); ``fabric`` (the ontology) is NOT.
    "go": {
        "pockets",
        "sessions",
        "agents",
        "memory",
        "studio",
        "sites",
        "instinct",
    },
    # Paw Pro — daily drivers. Adds automations + knowledge_base + display flags
    # for the power surfaces (code, deep_work, chain_flow, fleet, belt,
    # foresight). Keeps ``sites``; does NOT carry ``fabric`` (the ontology is
    # enterprise-only).
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
        "belt",
        "foresight",
        "instinct",
    },
    # Paw Pro Max — uncapped power users. Everything in pro (incl. belt +
    # foresight) + nothing extra at the feature-set level — the differentiator
    # is usage/price. Still NO ``fabric`` (enterprise-only ontology).
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
        "instinct",
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
