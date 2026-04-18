# ABAC policy context and result types — the data plane for authorization decisions.
# Created: 2026-04-10

from __future__ import annotations

from dataclasses import dataclass

from pocketpaw.ee.guards.rbac import PocketAccess, WorkspaceRole


@dataclass(frozen=True, slots=True)
class PolicyContext:
    """Everything a guard needs to make a decision."""

    user_id: str
    workspace_id: str
    role: WorkspaceRole
    action: str
    resource_id: str | None = None
    resource_type: str | None = None
    pocket_access: PocketAccess | None = None
    plan: str = "team"
    agent_id: str | None = None
    agent_creator_role: WorkspaceRole | None = None


@dataclass(frozen=True, slots=True)
class PolicyResult:
    """Outcome of a policy evaluation."""

    allowed: bool
    code: str = ""
    detail: str = ""
