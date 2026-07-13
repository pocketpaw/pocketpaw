# Single source of truth for RBAC action rules.
# Each action maps to the minimum role/access required and the stable
# machine-readable `code` emitted on denial. Tests iterate ACTIONS to
# guarantee every guarded operation is covered.
#
# Updated: 2026-07-11 (feat/external-alerting-c2c3) — registered
# ``automations.read`` (MEMBER) and ``automations.manage`` (ADMIN) for the
# always-on automation status surface (ee.cloud.automations_status.router): view
# the sweep registry / rules / per-workspace enable state, and flip the
# per-workspace opt-out.
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
#
# Updated: 2026-06-19 (feat/instinct-gate-integration, security-review FIX 1) —
# added ``instinct.activate`` (OWNER) gating the workspace route that sets a
# workspace's ``instinct_approval_level``. A non-ASK level turns on
# auto-approval of agent WRITE actions workspace-wide, so the switch is
# OWNER-only — the most restrictive workspace tier.
#
# Updated: 2026-06-10 (feat/belt-console-backend, SC-1) — added ``belt.read``
# (MEMBER) and ``belt.manage`` (ADMIN) so the Belt console router
# (ee.cloud.belt.router) can guard its read routes (repos list, runs list, run
# detail) and its add-repo route. ``belt.manage`` is ADMIN because adding a repo
# root extends the code-change security boundary workspace-wide — it must not be
# open to every member (mirrors connector.manage / skills.manage).
#
# Updated: 2026-07-01 (feat/sec-5-security-proxy, SEC-5) — added
# ``security.manage`` (OWNER) gating EVERY route on the shield control-plane
# proxy (ee.cloud.security.router). OWNER because the proxy fronts shield's
# ban-capable writes (resolve a decision, PATCH the egress deny/allow config)
# AND its read feed exposes who-tried-to-egress-what; the whole surface is
# owner-only, mirroring workspace.delete / billing.manage / instinct.activate.

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
    # Fabric — ontology read/write + schema authoring.
    # read/write are MEMBER so any workspace member can query objects and author
    # object DATA (create/update objects, add links). SCHEMA authoring — defining
    # object types, adding typed properties, declaring link types, and versioning
    # a type (ontology-operator-ux, the /fabric/schema surface) — is the more
    # privileged "operator" tier and is ADMIN: changing the ontology reshapes
    # write-time enforcement for the whole workspace, so it sits above the member
    # data tier (mirrors connector.manage / rules.manage / skills.manage).
    "fabric.read": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "fabric.write": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "fabric.admin": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Instinct — human-in-the-loop decision pipeline.
    # Propose and read are MEMBER (agents and analysts can propose + view actions).
    # Approve/reject and audit are ADMIN — governance actions with downstream
    # consequences (triggering automations, recording corrections).
    "instinct.read": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "instinct.propose": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "instinct.approve": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    "instinct.audit": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Activating the layered Instinct gate's triager (setting a workspace's
    # ``instinct_approval_level`` to a non-ASK value) turns ON AUTO-APPROVAL of
    # agent WRITE actions for the whole workspace — the single most sensitive
    # governance switch in the gate. OWNER-only, the most restrictive workspace
    # tier (mirrors workspace.delete / billing.manage): a mere admin must not
    # be able to disable the human-in-the-loop for everyone.
    "instinct.activate": ActionRule(WorkspaceRole.OWNER, "workspace.insufficient_role"),
    # Governed rules — the UI-authored guardrail surface (ee.cloud.rules.router:
    # create / list / archive a rule + the per-workspace enforcement toggle).
    # ADMIN, mirroring the other governance write surfaces (instinct.approve /
    # audit.read): authoring a rule and flipping enforcement change workspace-wide
    # governance and can only ADD blocks/escalations (never relax the template
    # floor), so an admin bar — not the OWNER-only bar reserved for
    # instinct.activate (which turns OFF the human-in-the-loop) — is correct.
    "rules.manage": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Automations status — the always-on automation surface (external-alerting C3).
    # read is MEMBER (any team member can view which sweeps/rules are running and
    # the per-workspace enable state). manage is ADMIN because flipping the
    # per-workspace opt-out turns the always-on background sweeps ON/OFF for the
    # whole workspace (mirrors rules.manage / connector.manage).
    "automations.read": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "automations.manage": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
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
    # Belt console — the develop-station read + repo-admin surface
    # (ee.cloud.belt.router, feat/belt-console-backend SC-1). read is MEMBER so
    # any team member can list discoverable repos + their own station runs.
    # manage is ADMIN because adding a repo root EXTENDS the code-change security
    # boundary workspace-wide (an admin authorizing where the agent may apply
    # diffs), mirroring connector.manage / skills.manage.
    "belt.read": ActionRule(WorkspaceRole.MEMBER, "workspace.insufficient_role"),
    "belt.manage": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Notifications external-delivery config (ee.cloud.notifications.router,
    # feat/external-alerting-delivery). ADMIN because the config sets where the
    # server POSTs on EVERY notification (a Slack / generic webhook URL) — it
    # extends the workspace's egress surface, mirroring connector.manage /
    # belt.manage. There is no separate read action: the config is admin-only to
    # view too (it holds the webhook URLs), so GET reuses ``notifications.manage``.
    "notifications.manage": ActionRule(WorkspaceRole.ADMIN, "workspace.insufficient_role"),
    # Security — the shield control-plane proxy (ee.cloud.security.router,
    # SEC-5). OWNER-only, the most restrictive workspace tier (mirrors
    # workspace.delete / billing.manage / instinct.activate). shield fronts the
    # ban-capable write endpoints (resolve a decision, PATCH the deny/allow
    # config) and IS the workspace's egress security gate, so read AND write
    # must both be owner-gated: a mere admin must not be able to see the
    # decision stream or flip the security posture for everyone. A single
    # action guards every route (reads included) because the decision feed
    # itself carries who-tried-to-egress-what, which is sensitive.
    "security.manage": ActionRule(WorkspaceRole.OWNER, "workspace.insufficient_role"),
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
