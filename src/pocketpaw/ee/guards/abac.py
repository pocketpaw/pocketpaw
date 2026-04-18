# Attribute-based policy rules — plan gates, action-role mapping, tool whitelist.
# Created: 2026-04-10

from __future__ import annotations

import logging

from pocketpaw.ee.guards.policy import PolicyContext, PolicyResult
from pocketpaw.ee.guards.rbac import WorkspaceRole

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Plan feature gates
# ---------------------------------------------------------------------------

PLAN_FEATURES: dict[str, set[str]] = {
    "team": {"pockets", "sessions", "agents", "memory"},
    "business": {
        "pockets",
        "sessions",
        "agents",
        "memory",
        "automations",
        "fabric",
        "knowledge_base",
    },
    "enterprise": {
        "pockets",
        "sessions",
        "agents",
        "memory",
        "automations",
        "fabric",
        "instinct",
        "knowledge_base",
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
