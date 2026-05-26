# Single source of truth for RBAC action rules.
# Each action maps to the minimum role/access required and the stable
# machine-readable `code` emitted on denial. Tests iterate ACTIONS to
# guarantee every guarded operation is covered.
#
# Updated: 2026-04-19 (fix/fleet-install-auth-guard) — registered
# ``fleet.install`` at ``WorkspaceRole.ADMIN`` with deny code
# ``workspace.insufficient_role``. This lets the fleet router call
# ``check_workspace_action`` (which already audits denials via
# ``log_denial``) instead of hand-rolling the role check — closes the
# P0 auth-bypass flagged in docs/plans/cluster-D-reality.md.
#
# Updated: 2026-05-06 (fix/rbac-connector-upload-guards) — registered
# ``connector.execute`` (MEMBER), ``connector.manage`` (ADMIN),
# ``uploads.write`` (MEMBER), and ``uploads.manage`` (ADMIN) so the
# connector and uploads routers can use ``require_action_any_workspace``
# instead of relying solely on ``require_license``.
#
# Updated: 2026-05-07 (fix/rbac-guards-fabric-instinct-agent-knowledge) —
# added ``fabric.read``, ``fabric.write``, ``instinct.read``,
# ``instinct.propose``, ``instinct.approve``, ``instinct.audit`` so the
# Fabric and Instinct routers (previously fully unguarded) can use
# ``require_action_any_workspace``.
#
# Updated: 2026-05-22 (feat/api-skills, Increment 2b) — added
# ``skills.manage`` (ADMIN) so the new ee.cloud.skills router can guard
# POST /skills/api-doc, the per-backend API-skill install endpoint.
#
# Updated: 2026-05-22 (RFC 05 M2b.2) — added ``outcomes.read`` (MEMBER) so
# the pocket-outcomes count router (ee.cloud.outcomes) can guard
# ``GET /api/v1/outcomes``.

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pocketpaw_ee.guards.rbac import Forbidden, PocketAccess, WorkspaceRole

# ---------------------------------------------------------------------------
# Group role — mirrors WorkspaceRole shape but scoped to a single group.
# Stored in Group.member_roles as "owner" | "admin" | "edit" | "view".
# "edit" maps to GroupRole.MEMBER, "view" is a posting restriction flag.
# ---------------------------------------------------------------------------


class GroupRole(StrEnum):
    VIEW = "view"
    MEMBER = "edit"
    ADMIN = "admin"
    OWNER = "owner"

    @classmethod
    def from_str(cls, value: str) -> GroupRole:
        try:
            return cls(value.lower())
        except ValueError:
            raise ValueError(f"Unknown group role: {value!r}") from None

    @property
    def level(self) -> int:
        return _GROUP_ROLE_LEVELS[self]


_GROUP_ROLE_LEVELS: dict[GroupRole, int] = {
    GroupRole.VIEW: 0,
    GroupRole.MEMBER: 1,
    GroupRole.ADMIN: 2,
    GroupRole.OWNER: 3,
}


def check_group_role(
    role: str | GroupRole,
    *,
    minimum: GroupRole,
    deny_code: str = "group.insufficient_role",
) -> None:
    """Raise Forbidden if role is below minimum."""
    resolved = role if isinstance(role, GroupRole) else GroupRole.from_str(role)
    if resolved.level < minimum.level:
        raise Forbidden(
            code=deny_code,
            detail=f"Requires {minimum.value}, got {resolved.value}",
        )


# ---------------------------------------------------------------------------
# Action rule
# ---------------------------------------------------------------------------


RoleType = WorkspaceRole | GroupRole | PocketAccess


@dataclass(frozen=True, slots=True)
class ActionRule:
    """A guarded action's minimum required role and deny code."""

    minimum: RoleType
    deny_code: str


# ---------------------------------------------------------------------------
# ACTIONS — the canonical matrix. Keep keys in dotted "resource.action" form.
# ---------------------------------------------------------------------------


ACTIONS: dict[str, ActionRule] = {
    # Workspace
    "workspace.view": ActionRule(WorkspaceRole.MEMBER, "workspace.not_member"),
    "workspace.update": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    "workspace.delete": ActionRule(WorkspaceRole.OWNER, "workspace.insufficient_role"),
    "workspace.transfer": ActionRule(WorkspaceRole.OWNER, "workspace.insufficient_role"),
    "workspace.invite": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    "workspace.member.remove": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    "workspace.member.role_change": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Group (chat)
    "group.view": ActionRule(GroupRole.VIEW, "group.not_member"),
    "group.create": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "channel.create": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    "group.post": ActionRule(GroupRole.MEMBER, "group.view_only"),
    "group.admin": ActionRule(GroupRole.ADMIN, "group.not_admin"),
    "group.delete": ActionRule(GroupRole.OWNER, "group.not_owner"),
    "group.transfer": ActionRule(GroupRole.OWNER, "group.not_owner"),
    # Message
    "message.edit_own": ActionRule(GroupRole.MEMBER, "message.not_author"),
    "message.delete_any": ActionRule(GroupRole.ADMIN, "group.not_admin"),
    # Pocket
    "pocket.read": ActionRule(PocketAccess.VIEW, "pocket.access_denied"),
    "pocket.comment": ActionRule(PocketAccess.COMMENT, "pocket.access_denied"),
    "pocket.edit": ActionRule(PocketAccess.EDIT, "pocket.access_denied"),
    "pocket.share": ActionRule(PocketAccess.OWNER, "pocket.not_owner"),
    "pocket.delete": ActionRule(PocketAccess.OWNER, "pocket.not_owner"),
    # Agent
    "agent.run": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "agent.create": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "agent.edit": ActionRule(WorkspaceRole.ADMIN, "agent.not_owner"),
    "agent.delete": ActionRule(WorkspaceRole.ADMIN, "agent.not_owner"),
    # Session
    "session.read_own": ActionRule(WorkspaceRole.MEMBER, "session.not_owner"),
    "session.read_any": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # KB
    "kb.read": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "kb.write": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    # Invite
    "invite.create": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    "invite.revoke": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    "invite.resend": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Billing
    "billing.view": ActionRule(WorkspaceRole.ADMIN, "billing.admin_only"),
    "billing.manage": ActionRule(WorkspaceRole.OWNER, "billing.owner_only"),
    # Fleet — spawning agents + pockets is a workspace-admin action.
    # Previously the install route had no auth guard at all, so any
    # authenticated caller could install into any workspace
    # (docs/plans/cluster-D-reality.md#106-112, P0 fix 2026-04-19).
    "fleet.install": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Fabric — ontology read/write.
    # Both tiers are MEMBER so any workspace member can query and author objects.
    # Type/schema management intentionally stays MEMBER for now; tighten to ADMIN
    # once a Fabric admin tier is validated with clients.
    "fabric.read": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "fabric.write": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    # Instinct — human-in-the-loop decision pipeline.
    # Propose and read are MEMBER (agents and analysts can propose + view actions).
    # Approve/reject and audit are ADMIN — governance actions with downstream
    # consequences (triggering automations, recording corrections).
    "instinct.read": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "instinct.propose": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "instinct.approve": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    "instinct.audit": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Connector — workspace-level connector lifecycle.
    # execute is MEMBER so any team member can run actions against enabled connectors.
    # manage (enable/disable/config) is ADMIN because it changes workspace-wide state
    # visible to all members and can trigger OAuth flows or expose credentials.
    "connector.execute": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "connector.manage": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Uploads — workspace-scoped file storage.
    # write is MEMBER so any team member can upload files or create folders.
    # manage is ADMIN for bulk moves, cross-user deletes, and storage policy changes.
    "uploads.write": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "uploads.manage": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Admin — operational endpoints (perf timing dumps, etc.). Owner-only
    # because per-route timing reveals traffic patterns and request
    # cadence that shouldn't be visible to every admin in a workspace.
    "admin.perf": ActionRule(WorkspaceRole.OWNER, "admin.access_denied"),
    # Audit — workspace-scoped audit log read surface (ee.cloud.audit).
    # ADMIN because audit entries can carry decision context, connector
    # payloads, and AI recommendations that should not be visible to
    # every workspace member. The role choice mirrors the existing
    # abac.ACTION_ROLES["audit.read"] entry.
    "audit.read": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Skills — installing a backend's OpenAPI spec as a per-backend API
    # skill (ee.cloud.skills). ADMIN, mirroring connector.manage: the
    # installed skill changes workspace-wide pocket-authoring behaviour
    # and the install accepts an uploaded document, so it should not be
    # open to every member.
    "skills.manage": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Outcomes — workspace-scoped pocket-outcome count surface
    # (ee.cloud.outcomes, RFC 05 M2b.2). MEMBER: an outcome count is a
    # non-sensitive activity metric (how many "renewal_completed" events a
    # pocket produced), with no credentials or decision payloads — any
    # workspace member may view it, mirroring instinct.read.
    "outcomes.read": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
}


def get_rule(action: str) -> ActionRule:
    """Fetch an action's rule. Raises KeyError if unknown (by design — unknown
    actions must fail loud, not silently allow)."""
    try:
        return ACTIONS[action]
    except KeyError:
        raise KeyError(
            f"Unknown action {action!r}. Register it in ACTIONS before guarding a route."
        ) from None


def check_action(
    action: str,
    actor_level: RoleType,
) -> None:
    """Raise Forbidden if actor_level is below the action's minimum.

    Both sides of the comparison must be the same enum family
    (WorkspaceRole vs. WorkspaceRole, PocketAccess vs. PocketAccess, etc.)
    — mixing families is a programming error.
    """
    rule = get_rule(action)
    if type(actor_level) is not type(rule.minimum):
        raise TypeError(
            f"Action {action!r} expects {type(rule.minimum).__name__}, "
            f"got {type(actor_level).__name__}"
        )
    if actor_level.level < rule.minimum.level:  # type: ignore[attr-defined]
        raise Forbidden(
            code=rule.deny_code,
            detail=(
                f"Action {action!r} requires {rule.minimum.value}, got {actor_level.value}"  # type: ignore[attr-defined]
            ),
        )
