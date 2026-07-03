# workspace_admin.py — in-process MCP server letting the cloud chat agent perform
#   WORKSPACE ADMINISTRATION, gated by the existing RBAC system (guards/), with
#   mutations routed through the Instinct approval gate (NEVER inline).
# Created: 2026-07-03 (feat/workspace-admin-tools, WA-1) — the first slice of the
#   workspace-admin tool surface. Mirrors the connectors.py / external_actions.py
#   shape: an SDK import-guard, ``SERVER_NAME`` / ``*_TOOL_ID`` allowlist
#   constants, ContextVar-sourced identity (the SAME ``current_workspace_id`` /
#   ``current_user_id`` accessors in ``ee.cloud.chat.agent_service`` the connector
#   / belt / sites servers read), and the ``_error_response`` / ``_success_response``
#   envelopes. Tool ids namespace as ``mcp__pocketpaw_workspace_admin__*``.
#
#   Two tools in WA-1:
#     * members_list() — READ. Gated at ``workspace.view`` (MEMBER — matches how
#       the REST list-members route gates it: any member may read the roster).
#       Resolves identity → loads the User doc → check_workspace_action → calls
#       ``workspace.service.list_members`` → structured envelope. A Forbidden is
#       CAUGHT and returned as a deny envelope (never raised out of the tool);
#       check_workspace_action already audits the denial via guards/audit.py.
#     * member_update_role(user_id, role) — WRITE, ADMIN-gated at
#       ``workspace.member.role_change``. THE load-bearing security rule: this
#       NEVER calls ``update_member_role`` inline. On an ADMIN pass it returns a
#       "proposed for approval" envelope WITHOUT firing the mutation.
#
# Updated: 2026-07-03 (feat/workspace-admin-tools, WA-2 — member_update_role now
#   files a LIVE Instinct proposal). The 8th gated proposal kind (the
#   ``_admin_action`` blob + ``ee.cloud.admin_proposals`` propose/executor +
#   router wiring on approve/reject/bulk) now exists, so member_update_role's
#   ADMIN pass calls ``admin_proposals.propose.propose_admin_action`` and returns
#   a "pending in the Tray" envelope carrying the proposal/action id. The role
#   change fires ONLY when a human approves the Action — and only after the
#   executor RE-CHECKS this proposer's CURRENT workspace role at approve time (a
#   since-demoted proposer's approved action fails closed). update_member_role is
#   STILL never called inline from this tool; the "pending_approval_unavailable"
#   stub is retired.
#
# Updated: 2026-07-03 (feat/workspace-admin-tools, WA-4 — five READ-only tools).
#   All EXECUTE directly once their RBAC gate passes (reads are not Instinct-gated
#   — that's writes only). Each mirrors members_list exactly: _identity() →
#   _load_user → check_workspace_action in a try/except that returns a deny
#   ENVELOPE (never raises) → existing service → _success_response. New tools and
#   the REST route each mirrors:
#     * workspace_settings_read() — READ workspace name/slug/plan/seats/branding.
#       Gate ``workspace.view`` (MEMBER — same read gate as members_list; the REST
#       GET /{id} uses require_membership). Service: workspace.service.get. Returns
#       a COMPACT view (no internal ids/secrets — owner user-id + branding asset
#       refs are intentionally omitted).
#     * invites_list() — READ pending invites. Gate ``invite.create`` (ADMIN —
#       EXACTLY what the REST GET /{id}/invites route gates on). Service:
#       workspace.service.list_invites.
#     * connectors_list() — READ connectors + enabled/connected status. Gate
#       ``workspace.view`` (MEMBER). The REST GET /connectors route is
#       membership-only (no explicit action) + does its own per-user connector-
#       permission filtering inside the service; workspace.view is the MEMBER read
#       gate that mirrors that membership requirement fail-closed. Service passes
#       user_id so the service applies the same permission filter the route does.
#     * billing_usage_read(start?, end?) — READ usage/spend. Gate ``workspace.view``
#       (MEMBER) — the REST GET /billing/usage route is membership-only (any role
#       may read its own workspace's usage; it does NOT gate on billing.manage), so
#       we match it and do NOT over-gate to OWNER. Service:
#       billing.usage.get_workspace_usage (start/end → start_date/end_date).
#     * audit_read(limit?) — READ recent audit rows. Gate ``audit.read`` (ADMIN —
#       EXACTLY what the REST GET /{id}/audit route gates on). Service:
#       audit.service.list_events_response (limit → AuditQueryRequest.limit).
#
#   OSS-EE boundary: this module imports only ``workspace.service`` +
#   ``guards.deps`` + the read services (connectors / billing.usage / audit) +
#   the User Beanie load, all lazily inside handlers, so the import surface stays
#   minimal (mirrors connectors.py's function-local imports).
#
# Updated: 2026-07-03 (feat/workspace-admin-tools, WA-5 — seven ADMIN WRITE tools).
#   Each PROPOSES through the existing ``_admin_action`` Instinct gate exactly like
#   member_update_role (WA-2) — validate args → RBAC-gate (deny envelope on
#   Forbidden, never raised) → ``propose_admin_action`` → PENDING envelope. NONE
#   mutates inline; the write fires only on human approval, after the executor
#   re-checks the proposer's CURRENT role. The new tools + their RBAC action + the
#   executor whitelist entry each extends (in admin_proposals/executor.py):
#     * member_remove(user_id) → ``workspace.member.remove`` (ADMIN) →
#       remove_member (cascades keys/sessions/member-data — the service's job).
#     * invite_create(email, role) → ``invite.create`` (ADMIN) → create_invite
#       (role constrained to admin|member).
#     * invite_revoke(invite_id) → ``invite.revoke`` (ADMIN — the REST revoke
#       route's action, NOT invite.create) → revoke_invite.
#     * connector_enable(name) / connector_disable(name) / connector_config(name,
#       config) → ``connector.manage`` (ADMIN) → the connectors enable / disable /
#       update_config services. connector_config's ``config`` is carried as an
#       OPAQUE dict the service validates — it never becomes top-level kwargs.
#     * workspace_update(name?, settings?, branding?) → ``workspace.update``
#       (ADMIN) → workspace.service.update. ONLY the recognized fields are
#       proposed; any stray key an agent adds is dropped here AND in the strict
#       adapter. The write-side helpers ``_gate_write`` + ``_propose_write`` are
#       the shared chokepoint (parallel to WA-4's ``_gate_read``).
#
# Updated: 2026-07-03 (feat/workspace-admin-tools, WA-6 — three OWNER WRITE tools).
#   The most security-sensitive tools in the surface: OWNER-only, destructive /
#   financial workspace ops. Each PROPOSES through the SAME ``_admin_action``
#   Instinct gate (validate → _gate_write deny-envelope → _propose_write pending),
#   NEVER mutates inline, and fires only on human approval after the executor
#   re-checks the proposer STILL holds OWNER. The new tools + their OWNER RBAC
#   action + the executor whitelist entry each extends:
#     * instinct_approval_level_set(level) → ``instinct.activate`` (OWNER) →
#       workspace.service.set_instinct_approval_level. ``level`` is constrained to
#       the canonical ``ApprovalLevel`` enum {ASK, TRIAGE, TRUSTED}; a non-ASK
#       level turns ON workspace-wide auto-approval of agent writes, so it's the
#       single most governance-sensitive switch — OWNER-gated + human-approved.
#     * workspace_delete() → ``workspace.delete`` (OWNER) →
#       workspace.service.delete. DESTRUCTIVE + IRREVERSIBLE (cascades every
#       member / room / agent / file). No args beyond identity. The envelope is
#       emphatic that it's irreversible and pending human approval — the tool only
#       proposes; the cascade fires on approval.
#     * billing_plan_change(plan) → ``billing.manage`` (OWNER) →
#       billing.service.subscribe. IMPORTANT (payment honesty): a paid-plan change
#       flows through Dodo's HOSTED CHECKOUT — the plan flips ONLY when Dodo posts
#       a verified ``subscription.active`` webhook, NOT synchronously. So the
#       executor does NOT fake a plan mutation: on approval it calls ``subscribe``,
#       which returns a ``{checkout_url}`` the human must complete. The webhook-
#       internal ``set_workspace_plan`` (which bypasses payment) is DELIBERATELY
#       NOT wired — an agent must never flip a paid plan without the real payment
#       flow. ``plan`` is constrained to the plan catalog keys.
#     * seats_manage — DELIBERATELY NOT BUILT. There is NO seat-change service and
#       no billing/checkout seat flow anywhere in the codebase (seats are a
#       workspace-doc field set at creation; ``UpdateWorkspaceRequest`` has no
#       ``seats`` field). With no safe, honest execution path, wiring it would mean
#       either a silent seat DB write (bypassing billing) or a fabricated flow —
#       both refused. Skipped; escalated as NEEDS_CONTEXT.
"""Agent-side MCP surface for workspace administration.

Tools registered:

  - ``members_list()`` — READ the current workspace's member roster (email,
    name, role, joined-at). Gated at ``workspace.view`` (any member may read).
    No arguments — the workspace is inferred from the active chat stream. A
    Forbidden is returned as a structured deny envelope, never raised.
  - ``member_update_role(user_id, role)`` — WRITE: change a member's workspace
    role. ADMIN-gated at ``workspace.member.role_change``. This does NOT mutate
    inline — an admin write is proposed for human approval through the Instinct
    Tray (WA-2: a live ``_admin_action`` proposal) and fires only on approval,
    after an execute-time re-check of the proposer's CURRENT role. Returns a
    "pending in the Tray" envelope carrying the proposal/action id.
  - ``workspace_settings_read()`` — READ the workspace's name / slug / plan /
    seat usage / branding. Gated at ``workspace.view`` (MEMBER). Compact view —
    no internal ids or secrets.
  - ``invites_list()`` — READ pending invites. Gated at ``invite.create``
    (ADMIN — same as the REST invites route).
  - ``connectors_list()`` — READ connectors + enabled/connected status. Gated at
    ``workspace.view`` (MEMBER); the service applies the caller's connector
    permissions.
  - ``billing_usage_read(start, end)`` — READ daily usage/spend over an optional
    date window. Gated at ``workspace.view`` (MEMBER — matching the
    membership-only REST usage route).
  - ``audit_read(limit)`` — READ recent audit-log rows. Gated at ``audit.read``
    (ADMIN — same as the REST audit route).

All READ tools EXECUTE directly once their RBAC gate passes (reads are not
Instinct-gated). A Forbidden is returned as a structured deny envelope, never
raised.

Identity comes from ``agent_service.current_workspace_id`` /
``current_user_id``. Outside an SSE chat stream those are empty → the tools
return a clear error rather than silently mis-tenanting.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_workspace_admin"
# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
# Allowlist entries must use this exact form.
MEMBERS_LIST_TOOL_ID = f"mcp__{SERVER_NAME}__members_list"
MEMBER_UPDATE_ROLE_TOOL_ID = f"mcp__{SERVER_NAME}__member_update_role"
WORKSPACE_SETTINGS_READ_TOOL_ID = f"mcp__{SERVER_NAME}__workspace_settings_read"
INVITES_LIST_TOOL_ID = f"mcp__{SERVER_NAME}__invites_list"
CONNECTORS_LIST_TOOL_ID = f"mcp__{SERVER_NAME}__connectors_list"
BILLING_USAGE_READ_TOOL_ID = f"mcp__{SERVER_NAME}__billing_usage_read"
AUDIT_READ_TOOL_ID = f"mcp__{SERVER_NAME}__audit_read"
# WA-5 — ADMIN WRITE tools. Each PROPOSES through the ``_admin_action`` Instinct
# gate (never mutates inline), exactly like member_update_role.
MEMBER_REMOVE_TOOL_ID = f"mcp__{SERVER_NAME}__member_remove"
INVITE_CREATE_TOOL_ID = f"mcp__{SERVER_NAME}__invite_create"
INVITE_REVOKE_TOOL_ID = f"mcp__{SERVER_NAME}__invite_revoke"
CONNECTOR_ENABLE_TOOL_ID = f"mcp__{SERVER_NAME}__connector_enable"
CONNECTOR_DISABLE_TOOL_ID = f"mcp__{SERVER_NAME}__connector_disable"
CONNECTOR_CONFIG_TOOL_ID = f"mcp__{SERVER_NAME}__connector_config"
WORKSPACE_UPDATE_TOOL_ID = f"mcp__{SERVER_NAME}__workspace_update"
# WA-6 — OWNER WRITE tools (the most security-sensitive: destructive / financial /
# governance ops). Each PROPOSES through the ``_admin_action`` Instinct gate
# exactly like the ADMIN writes, but gates on an OWNER RBAC action.
INSTINCT_APPROVAL_LEVEL_SET_TOOL_ID = f"mcp__{SERVER_NAME}__instinct_approval_level_set"
WORKSPACE_DELETE_TOOL_ID = f"mcp__{SERVER_NAME}__workspace_delete"
BILLING_PLAN_CHANGE_TOOL_ID = f"mcp__{SERVER_NAME}__billing_plan_change"

ADMIN_TOOL_IDS = (
    MEMBERS_LIST_TOOL_ID,
    MEMBER_UPDATE_ROLE_TOOL_ID,
    WORKSPACE_SETTINGS_READ_TOOL_ID,
    INVITES_LIST_TOOL_ID,
    CONNECTORS_LIST_TOOL_ID,
    BILLING_USAGE_READ_TOOL_ID,
    AUDIT_READ_TOOL_ID,
    MEMBER_REMOVE_TOOL_ID,
    INVITE_CREATE_TOOL_ID,
    INVITE_REVOKE_TOOL_ID,
    CONNECTOR_ENABLE_TOOL_ID,
    CONNECTOR_DISABLE_TOOL_ID,
    CONNECTOR_CONFIG_TOOL_ID,
    WORKSPACE_UPDATE_TOOL_ID,
    INSTINCT_APPROVAL_LEVEL_SET_TOOL_ID,
    WORKSPACE_DELETE_TOOL_ID,
    BILLING_PLAN_CHANGE_TOOL_ID,
)

# The RBAC action keys these tools gate on (canonical entries in
# ``guards.actions.ACTIONS``). ``workspace.view`` = MEMBER (read the roster /
# settings / connectors / usage), ``workspace.member.role_change`` = ADMIN
# (change a member's role), ``invite.create`` = ADMIN (read/manage invites —
# the REST invites route's action), ``audit.read`` = ADMIN (read the audit log).
_READ_ACTION = "workspace.view"
_ROLE_CHANGE_ACTION = "workspace.member.role_change"
_INVITE_ACTION = "invite.create"
_AUDIT_ACTION = "audit.read"
# WA-5 ADMIN WRITE actions (all ADMIN in guards.actions.ACTIONS). The tool RBAC-
# gates on these; the executor whitelists + re-checks the SAME keys.
_MEMBER_REMOVE_ACTION = "workspace.member.remove"
_INVITE_REVOKE_ACTION = "invite.revoke"  # the REST revoke route's action (not invite.create)
_CONNECTOR_ACTION = "connector.manage"
_WORKSPACE_UPDATE_ACTION = "workspace.update"
# WA-6 OWNER WRITE actions (all OWNER in guards.actions.ACTIONS — the top tier,
# mirroring each other). The tool RBAC-gates on these; the executor whitelists +
# re-checks the SAME keys (a proposer demoted from OWNER after proposing fails
# closed at approve time).
_INSTINCT_ACTIVATE_ACTION = "instinct.activate"
_WORKSPACE_DELETE_ACTION = "workspace.delete"
_BILLING_MANAGE_ACTION = "billing.manage"

# The roles a member may be INVITED as (mirrors CreateInviteRequest / the REST
# invite DTO — an invite can never mint an owner).
_VALID_INVITE_ROLES = frozenset({"admin", "member"})

# The workspace roles a member may be assigned via member_update_role. Kept in
# sync with ``guards.rbac.WorkspaceRole`` — an out-of-set value is refused
# before any gate check (defense in depth; the tool schema also constrains it).
_VALID_ROLES = frozenset({"member", "admin", "owner"})

# The Instinct-gate activation levels a workspace may be set to. Kept in sync with
# ``cloud.pockets.instinct_triage.ApprovalLevel`` (a StrEnum: ASK / TRIAGE /
# TRUSTED). A non-ASK level turns ON workspace-wide auto-approval of agent WRITE
# actions — the single most governance-sensitive switch — so an out-of-set value
# is refused before any gate (defense in depth; the service re-validates too).
_VALID_APPROVAL_LEVELS = frozenset({"ASK", "TRIAGE", "TRUSTED"})

# The plan tiers ``billing_plan_change`` may target. Mirrors the billing plan
# catalog (cloud.billing.plans). ``subscribe`` re-validates against the live
# catalog and refuses an unconfigured tier — this frozenset is the tool's own
# fast-fail so an obvious typo never opens a proposal.
_VALID_PLANS = frozenset({"free", "go", "pro", "pro_max", "enterprise"})


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape the SDK expects."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    """Build an MCP success response carrying ``body`` as JSON."""
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(body, separators=(",", ":"), default=str),
            }
        ]
    }


def _identity() -> tuple[str | None, str | None, str | None]:
    """Resolve workspace / user / pocket from the per-stream ContextVars set by
    ``run_core`` via ``agent_service.attach_agent_identity`` — the same
    chokepoint the connectors / belt / sites servers read.

    Returns ``(workspace_id, user_id, pocket_id)``. Any may be ``None`` when the
    tool is called outside an SSE chat stream (e.g. a unit test) — the handlers
    treat a missing workspace as "no room context" and refuse rather than
    silently mis-tenanting.
    """
    try:
        from pocketpaw_ee.cloud.chat.agent_service import (
            current_pocket_id,
            current_user_id,
            current_workspace_id,
        )

        return current_workspace_id(), current_user_id(), current_pocket_id()
    except Exception:  # noqa: BLE001 — agent_service import only fails off-stream
        return None, None, None


async def _load_user(user_id: str) -> Any | None:
    """Load the User Beanie doc for ``user_id`` so ``check_workspace_action`` has
    the ``.workspaces`` membership list it reads. Returns ``None`` on a bad id /
    missing user — the caller maps that to an error envelope. Lazy import keeps
    the module's top-level import surface minimal (mirrors connectors.py)."""
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.user import User as _UserDoc

    try:
        return await _UserDoc.get(PydanticObjectId(user_id))
    except Exception:  # noqa: BLE001 — malformed id / DB error → treat as no user
        return None


def _legacy_ctx(user_id: str, workspace_id: str) -> Any:
    """Build a minimal ``RequestContext`` for the workspace service read path.

    ``workspace.service.list_members`` takes a ``ctx`` and reads ``ctx.user_id``
    (for the membership self-check) — the workspace is passed explicitly. We
    build the context from the resolved identity. Lazy import to keep the top
    level free of an ee.cloud dependency."""
    from datetime import UTC, datetime

    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind

    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="mcp-workspace-admin",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _gate_read(
    tool: str, action: str, deny_message: str
) -> tuple[str, str, None] | dict:
    """Shared identity-resolve + RBAC-gate for a READ tool.

    Returns ``(workspace_id, user_id, None)`` when the gate PASSES, or a ready-to-
    return response dict when it fails (no active workspace / unresolvable user /
    Forbidden). Mirrors ``members_list``'s handling EXACTLY: a Forbidden is CAUGHT
    and returned as a structured deny envelope (never raised); the gate already
    audits the denial via ``guards/audit.log_denial``. Callers branch on the
    return type: a ``tuple`` means proceed, a ``dict`` means return it.
    """
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            f"no active workspace — {tool} can only be called from inside a cloud "
            "chat stream (it has no workspace context outside the chat stream)."
        )

    from pocketpaw_ee.guards.deps import check_workspace_action
    from pocketpaw_ee.guards.rbac import Forbidden

    user = await _load_user(user_id)
    if user is None:
        return _error_response("could not resolve the calling user for the RBAC check.")

    try:
        check_workspace_action(user, workspace_id, action)
    except Forbidden as exc:
        logger.info(
            "%s denied: user=%s workspace=%s code=%s",
            tool,
            user_id,
            workspace_id,
            exc.code,
        )
        return _success_response(
            {
                "ok": False,
                "denied": True,
                "code": exc.code,
                "message": f"{deny_message} ({exc.code}).",
            }
        )

    return workspace_id, user_id, None


async def _gate_write(
    tool: str, action: str, deny_message: str
) -> tuple[str, str, None] | dict:
    """Shared identity-resolve + RBAC-gate for a WRITE tool.

    IDENTICAL control flow to ``_gate_read`` — a Forbidden is CAUGHT and returned
    as a deny envelope, never raised; the gate audits the denial. The difference
    is purely semantic: a write tool that passes this gate does NOT execute — it
    goes on to ``propose_admin_action`` (WA-2). Kept as its own name so the call
    sites read as writes. Returns ``(workspace_id, user_id, None)`` on PASS or a
    ready-to-return response ``dict`` on any failure."""
    return await _gate_read(tool, action, deny_message)


async def _propose_write(
    *,
    tool: str,
    action: str,
    workspace_id: str,
    user_id: str,
    args: dict[str, Any],
    summary: str,
    title: str,
    proposed_change: dict[str, Any],
    what: str,
) -> dict:
    """File a live ``_admin_action`` proposal and return a PENDING envelope.

    The single write-side chokepoint (mirrors member_update_role's WA-2 tail):
    the tool NEVER mutates inline — it proposes and returns "pending in the
    Tray". ``args`` is the STRICT arg set the executor's adapter will read (only
    the keys the matching ``_adapt_*`` expects). ``proposed_change`` is the
    human-facing echo. ``what`` is a short noun phrase for the relay message."""
    from pocketpaw_ee.cloud.admin_proposals.propose import propose_admin_action

    try:
        action_id = await propose_admin_action(
            workspace_id=workspace_id,
            action=action,
            args=args,
            proposer_user_id=user_id,
            summary=summary,
            title=title,
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean MCP error, never a 500
        logger.warning("%s: propose_admin_action failed", tool, exc_info=True)
        return _error_response(f"could not file the {what} for approval: {exc}")

    logger.info(
        "%s PROPOSED (WA-5): actor=%s workspace=%s action=%s proposal=%s — "
        "pending human approval in the Tray",
        tool,
        user_id,
        workspace_id,
        action,
        action_id,
    )
    return _success_response(
        {
            "ok": True,
            "executed": False,
            "status": "pending_approval",
            "action_id": action_id,
            "proposal_id": action_id,
            "proposed_change": proposed_change,
            "message": (
                f"This is an admin action ({what}) and must be approved by a human "
                "— it is never applied directly from chat. I've proposed it; it's "
                "now PENDING approval in the Tray and will take effect only once a "
                "human approves it. No change has been made yet. Tell the user "
                "you've requested it and it needs approval; do NOT claim it's done."
            ),
        }
    )


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def _members_list_handler(args: dict) -> dict:  # noqa: ARG001 — no args
    """READ the workspace member roster. Gated at ``workspace.view`` (MEMBER)."""
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — members_list can only be called from inside a "
            "cloud chat stream (it has no workspace context outside the chat "
            "stream)."
        )

    from pocketpaw_ee.guards.deps import check_workspace_action
    from pocketpaw_ee.guards.rbac import Forbidden

    user = await _load_user(user_id)
    if user is None:
        return _error_response("could not resolve the calling user for the RBAC check.")

    # RBAC gate — MEMBER may read the roster. A Forbidden is CAUGHT and returned
    # as a structured deny envelope (never raised out of the tool). The gate
    # already audits the denial via guards/audit.log_denial.
    try:
        check_workspace_action(user, workspace_id, _READ_ACTION)
    except Forbidden as exc:
        logger.info(
            "members_list denied: user=%s workspace=%s code=%s",
            user_id,
            workspace_id,
            exc.code,
        )
        return _success_response(
            {
                "ok": False,
                "denied": True,
                "code": exc.code,
                "message": (
                    "You don't have permission to view this workspace's members "
                    f"({exc.code})."
                ),
            }
        )

    from pocketpaw_ee.cloud.workspace import service as workspace_service

    try:
        members = await workspace_service.list_members(
            _legacy_ctx(user_id, workspace_id), workspace_id
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean MCP error, never a 500
        logger.warning("members_list failed", exc_info=True)
        return _error_response(f"members_list failed: {exc}")

    return _success_response(
        {
            "ok": True,
            "workspace_id": workspace_id,
            "members": [
                {
                    "user_id": m.user_id,
                    "email": m.email,
                    "name": m.name,
                    "role": m.role,
                    "joined_at": m.joined_at,
                }
                for m in members
            ],
            "count": len(members),
        }
    )


async def _member_update_role_handler(args: dict) -> dict:
    """WRITE: change a member's workspace role. ADMIN-gated; Instinct-proposed.

    THE load-bearing security rule: this NEVER calls ``update_member_role``
    inline. On an ADMIN pass it files a live Instinct ``_admin_action`` proposal
    (WA-2) via ``admin_proposals.propose.propose_admin_action`` and returns a
    "pending in the Tray" envelope carrying the proposal/action id — WITHOUT
    firing the mutation. The role change fires only when a human approves the
    Action, and only after the executor re-checks THIS proposer's CURRENT role.
    """
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — member_update_role can only be called from "
            "inside a cloud chat stream."
        )

    target_user_id = args.get("user_id")
    if not isinstance(target_user_id, str) or not target_user_id.strip():
        return _error_response("user_id is required (string — the member to change).")
    role = args.get("role")
    if not isinstance(role, str) or role not in _VALID_ROLES:
        return _error_response(
            f"role is required and must be one of {sorted(_VALID_ROLES)}."
        )

    from pocketpaw_ee.guards.deps import check_workspace_action
    from pocketpaw_ee.guards.rbac import Forbidden

    user = await _load_user(user_id)
    if user is None:
        return _error_response("could not resolve the calling user for the RBAC check.")

    # RBAC gate — ADMIN required. A Forbidden is CAUGHT and returned as a deny
    # envelope (never raised); the gate audits the denial via log_denial. The
    # deny is enforced BEFORE any proposal/mutation path — a member never
    # reaches the write.
    try:
        check_workspace_action(user, workspace_id, _ROLE_CHANGE_ACTION)
    except Forbidden as exc:
        logger.info(
            "member_update_role denied: actor=%s workspace=%s target=%s code=%s",
            user_id,
            workspace_id,
            target_user_id,
            exc.code,
        )
        return _success_response(
            {
                "ok": False,
                "denied": True,
                "code": exc.code,
                "message": (
                    "You don't have permission to change member roles in this "
                    f"workspace ({exc.code})."
                ),
            }
        )

    # ADMIN passed. DO NOT MUTATE INLINE — an admin write is human-gated. WA-2:
    # file a real Instinct proposal carrying an ``_admin_action`` blob and return
    # a "pending in Tray" envelope. The mutation fires ONLY when a human approves
    # the Action in the Tray — and only after the executor re-checks THIS
    # proposer's CURRENT role at approve time. update_member_role is NEVER called
    # from here.
    from pocketpaw_ee.cloud.admin_proposals.propose import propose_admin_action

    try:
        action_id = await propose_admin_action(
            workspace_id=workspace_id,
            action=_ROLE_CHANGE_ACTION,
            args={"target_user_id": target_user_id, "role": role},
            proposer_user_id=user_id,
            summary=f"Change member {target_user_id} to role '{role}'.",
            title=f"Role change — member {target_user_id} → {role}",
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean MCP error, never a 500
        logger.warning("member_update_role: propose_admin_action failed", exc_info=True)
        return _error_response(f"could not file the role change for approval: {exc}")

    logger.info(
        "member_update_role PROPOSED (WA-2): actor=%s workspace=%s target=%s "
        "role=%s action=%s — pending human approval in the Tray",
        user_id,
        workspace_id,
        target_user_id,
        role,
        action_id,
    )
    return _success_response(
        {
            "ok": True,
            "executed": False,
            "status": "pending_approval",
            "action_id": action_id,
            "proposal_id": action_id,
            "proposed_change": {
                "user_id": target_user_id,
                "role": role,
                "workspace_id": workspace_id,
            },
            "message": (
                "Role changes are admin-gated and must be approved by a human — "
                "they are never applied directly from chat. I've proposed this "
                "change; it's now PENDING approval in the Tray and will take "
                "effect only once a human approves it. No change has been made "
                "yet. Tell the user you've requested the change and it needs "
                "approval; do NOT claim the role was changed."
            ),
        }
    )


async def _workspace_settings_read_handler(args: dict) -> dict:  # noqa: ARG001 — no args
    """READ the workspace's name / slug / plan / seats / branding. Gated at
    ``workspace.view`` (MEMBER). EXECUTES directly on a gate pass (a read is not
    Instinct-gated). Returns a COMPACT view — no internal ids or secrets (the
    owner user-id and branding asset refs are intentionally omitted)."""
    gate = await _gate_read(
        "workspace_settings_read",
        _READ_ACTION,
        "You don't have permission to view this workspace's settings",
    )
    if isinstance(gate, dict):
        return gate
    workspace_id, user_id, _ = gate

    from pocketpaw_ee.cloud.workspace import service as workspace_service

    try:
        ws = await workspace_service.get(_legacy_ctx(user_id, workspace_id), workspace_id)
    except Exception as exc:  # noqa: BLE001 — surface a clean MCP error, never a 500
        logger.warning("workspace_settings_read failed", exc_info=True)
        return _error_response(f"workspace_settings_read failed: {exc}")

    branding = None
    if ws.branding is not None:
        # Only the display-facing branding fields — asset refs (logo/favicon
        # storage ids) are internal and intentionally omitted.
        branding = {
            "display_name": ws.branding.display_name,
            "tab_title": ws.branding.tab_title,
            "accent_color": ws.branding.accent_color,
            "show_paw_mark": ws.branding.show_paw_mark,
        }

    return _success_response(
        {
            "ok": True,
            "workspace_id": workspace_id,
            "name": ws.name,
            "slug": ws.slug,
            "plan": ws.plan,
            "seats": ws.seats,
            "member_count": ws.member_count,
            "seats_used": ws.member_count,
            "seats_available": max(ws.seats - ws.member_count, 0),
            "branding": branding,
        }
    )


async def _invites_list_handler(args: dict) -> dict:  # noqa: ARG001 — no args
    """READ the workspace's PENDING invites. Gated at ``invite.create`` (ADMIN —
    the same action the REST GET /{id}/invites route gates on). EXECUTES directly
    on a gate pass."""
    gate = await _gate_read(
        "invites_list",
        _INVITE_ACTION,
        "You don't have permission to view this workspace's invites",
    )
    if isinstance(gate, dict):
        return gate
    workspace_id, _user_id, _ = gate

    from pocketpaw_ee.cloud.workspace import service as workspace_service

    try:
        invites = await workspace_service.list_invites(workspace_id)
    except Exception as exc:  # noqa: BLE001 — surface a clean MCP error, never a 500
        logger.warning("invites_list failed", exc_info=True)
        return _error_response(f"invites_list failed: {exc}")

    return _success_response(
        {
            "ok": True,
            "workspace_id": workspace_id,
            "invites": [
                {
                    "id": inv.id,
                    "email": inv.email,
                    "role": inv.role,
                    "invited_by": inv.invited_by,
                    "group_id": inv.group_id,
                    "expires_at": inv.expires_at,
                }
                for inv in invites
            ],
            "count": len(invites),
        }
    )


async def _connectors_list_handler(args: dict) -> dict:  # noqa: ARG001 — no args
    """READ the connectors available/enabled in this workspace + their connected
    status. Gated at ``workspace.view`` (MEMBER); the service applies the caller's
    per-user connector permissions (the same filter the REST route runs). EXECUTES
    directly on a gate pass."""
    gate = await _gate_read(
        "connectors_list",
        _READ_ACTION,
        "You don't have permission to view this workspace's connectors",
    )
    if isinstance(gate, dict):
        return gate
    workspace_id, user_id, _ = gate

    from pocketpaw_ee.cloud.connectors import service as connectors_service

    try:
        connectors = await connectors_service.list_connectors(workspace_id, user_id=user_id)
    except Exception as exc:  # noqa: BLE001 — surface a clean MCP error, never a 500
        logger.warning("connectors_list failed", exc_info=True)
        return _error_response(f"connectors_list failed: {exc}")

    return _success_response(
        {
            "ok": True,
            "workspace_id": workspace_id,
            "connectors": [
                {
                    "name": c.name,
                    "display_name": c.display_name,
                    "type": c.type,
                    "enabled": c.enabled,
                    "status": c.status,
                    "last_sync_status": c.last_sync_status,
                    "last_sync_at": c.last_sync_at,
                }
                for c in connectors
            ],
            "count": len(connectors),
        }
    )


async def _billing_usage_read_handler(args: dict) -> dict:
    """READ the workspace's daily usage/spend over an optional date window. Gated
    at ``workspace.view`` (MEMBER — the REST GET /billing/usage route is
    membership-only; it does NOT gate on billing.manage, so we match it and do NOT
    over-gate to OWNER). EXECUTES directly on a gate pass. ``start`` / ``end`` are
    optional ``YYYY-MM-DD`` strings (default: the trailing 30 days)."""
    start = args.get("start")
    end = args.get("end")
    if start is not None and not isinstance(start, str):
        return _error_response("start must be a YYYY-MM-DD date string when provided.")
    if end is not None and not isinstance(end, str):
        return _error_response("end must be a YYYY-MM-DD date string when provided.")

    gate = await _gate_read(
        "billing_usage_read",
        _READ_ACTION,
        "You don't have permission to view this workspace's usage",
    )
    if isinstance(gate, dict):
        return gate
    workspace_id, _user_id, _ = gate

    from pocketpaw_ee.cloud.billing import usage as usage_service

    try:
        usage = await usage_service.get_workspace_usage(
            workspace_id, start_date=start, end_date=end
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean MCP error, never a 500
        logger.warning("billing_usage_read failed", exc_info=True)
        return _error_response(f"billing_usage_read failed: {exc}")

    return _success_response(
        {
            "ok": True,
            "workspace_id": workspace_id,
            "start_date": usage.start_date,
            "end_date": usage.end_date,
            "models": usage.models,
            "total_credits": usage.total_credits,
            "buckets": [
                {
                    "date": b.date,
                    "total_credits": b.total_credits,
                    "by_model": {
                        model: {
                            "credits": stats.credits,
                            "requests": stats.requests,
                            "tokens": stats.tokens,
                        }
                        for model, stats in b.by_model.items()
                    },
                }
                for b in usage.buckets
            ],
        }
    )


async def _audit_read_handler(args: dict) -> dict:
    """READ recent audit-log rows for the workspace. Gated at ``audit.read``
    (ADMIN — the same action the REST GET /{id}/audit route gates on). EXECUTES
    directly on a gate pass. ``limit`` is optional (1–100; the service defaults to
    50 and clamps out-of-range values)."""
    limit = args.get("limit")
    if limit is not None and not isinstance(limit, int):
        return _error_response("limit must be an integer (1–100) when provided.")

    gate = await _gate_read(
        "audit_read",
        _AUDIT_ACTION,
        "You don't have permission to view this workspace's audit log",
    )
    if isinstance(gate, dict):
        return gate
    workspace_id, _user_id, _ = gate

    from pocketpaw_ee.cloud.audit import service as audit_service
    from pocketpaw_ee.cloud.audit.dto import AuditQueryRequest

    # The DTO validator constrains limit to 1–100 (422 on out-of-range at the
    # route boundary). Build it defensively so a bad limit maps to a clean MCP
    # error rather than an exception.
    try:
        query = AuditQueryRequest(limit=limit) if limit is not None else AuditQueryRequest()
    except Exception as exc:  # noqa: BLE001 — pydantic validation → clean error
        return _error_response(f"invalid limit: {exc}")

    try:
        page = await audit_service.list_events_response(workspace_id, query)
    except Exception as exc:  # noqa: BLE001 — surface a clean MCP error, never a 500
        logger.warning("audit_read failed", exc_info=True)
        return _error_response(f"audit_read failed: {exc}")

    return _success_response(
        {
            "ok": True,
            "workspace_id": workspace_id,
            "items": [
                {
                    "id": item.id,
                    "actor_id": item.actorId,
                    "action": item.action,
                    "target_type": item.targetType,
                    "target_id": item.targetId,
                    "metadata": item.metadata,
                    "at": item.at,
                }
                for item in page.items
            ],
            "count": len(page.items),
            "next_cursor": page.nextCursor,
        }
    )


# ---------------------------------------------------------------------------
# WA-5 ADMIN WRITE tool handlers — each PROPOSES through the ``_admin_action``
# gate (never mutates inline). Shape: validate args → _gate_write (deny envelope
# on Forbidden) → _propose_write (pending envelope). The mutation fires ONLY on
# human approval, after the executor re-checks the proposer's CURRENT role.
# ---------------------------------------------------------------------------


async def _member_remove_handler(args: dict) -> dict:
    """WRITE: remove a member from the workspace. ADMIN-gated; Instinct-proposed.

    The service cascade (API keys, sessions, member data) is the service's job —
    the tool only proposes it. The proposal carries ONLY the target user id."""
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — member_remove can only be called from inside a "
            "cloud chat stream."
        )
    target_user_id = args.get("user_id")
    if not isinstance(target_user_id, str) or not target_user_id.strip():
        return _error_response("user_id is required (string — the member to remove).")

    gate = await _gate_write(
        "member_remove",
        _MEMBER_REMOVE_ACTION,
        "You don't have permission to remove members from this workspace",
    )
    if isinstance(gate, dict):
        return gate

    return await _propose_write(
        tool="member_remove",
        action=_MEMBER_REMOVE_ACTION,
        workspace_id=workspace_id,
        user_id=user_id,
        args={"target_user_id": target_user_id},
        summary=f"Remove member {target_user_id} from the workspace.",
        title=f"Remove member {target_user_id}",
        proposed_change={"user_id": target_user_id, "workspace_id": workspace_id},
        what="member removal",
    )


async def _invite_create_handler(args: dict) -> dict:
    """WRITE: invite someone to the workspace. ADMIN-gated; Instinct-proposed.

    The proposal carries ONLY email + role (constrained to admin | member — an
    invite can't mint an owner)."""
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — invite_create can only be called from inside a "
            "cloud chat stream."
        )
    email = args.get("email")
    if not isinstance(email, str) or not email.strip():
        return _error_response("email is required (string — who to invite).")
    role = args.get("role", "member")
    if not isinstance(role, str) or role not in _VALID_INVITE_ROLES:
        return _error_response(
            f"role must be one of {sorted(_VALID_INVITE_ROLES)} (an invite can't be owner)."
        )

    gate = await _gate_write(
        "invite_create",
        _INVITE_ACTION,
        "You don't have permission to invite members to this workspace",
    )
    if isinstance(gate, dict):
        return gate

    return await _propose_write(
        tool="invite_create",
        action=_INVITE_ACTION,
        workspace_id=workspace_id,
        user_id=user_id,
        args={"email": email, "role": role},
        summary=f"Invite {email} as '{role}'.",
        title=f"Invite {email} → {role}",
        proposed_change={"email": email, "role": role, "workspace_id": workspace_id},
        what="invite",
    )


async def _invite_revoke_handler(args: dict) -> dict:
    """WRITE: revoke a pending invite. ADMIN-gated (``invite.revoke`` — the same
    action the REST revoke route uses); Instinct-proposed. Carries ONLY the
    invite id."""
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — invite_revoke can only be called from inside a "
            "cloud chat stream."
        )
    invite_id = args.get("invite_id")
    if not isinstance(invite_id, str) or not invite_id.strip():
        return _error_response("invite_id is required (string — from invites_list).")

    gate = await _gate_write(
        "invite_revoke",
        _INVITE_REVOKE_ACTION,
        "You don't have permission to revoke invites in this workspace",
    )
    if isinstance(gate, dict):
        return gate

    return await _propose_write(
        tool="invite_revoke",
        action=_INVITE_REVOKE_ACTION,
        workspace_id=workspace_id,
        user_id=user_id,
        args={"invite_id": invite_id},
        summary=f"Revoke invite {invite_id}.",
        title=f"Revoke invite {invite_id}",
        proposed_change={"invite_id": invite_id, "workspace_id": workspace_id},
        what="invite revocation",
    )


async def _connector_enable_handler(args: dict) -> dict:
    """WRITE: enable a connector for the workspace. ADMIN-gated
    (``connector.manage``); Instinct-proposed. Carries op=enable + name."""
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — connector_enable can only be called from inside "
            "a cloud chat stream."
        )
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return _error_response("name is required (string — the connector to enable).")

    gate = await _gate_write(
        "connector_enable",
        _CONNECTOR_ACTION,
        "You don't have permission to manage connectors in this workspace",
    )
    if isinstance(gate, dict):
        return gate

    return await _propose_write(
        tool="connector_enable",
        action=_CONNECTOR_ACTION,
        workspace_id=workspace_id,
        user_id=user_id,
        args={"op": "enable", "name": name},
        summary=f"Enable the '{name}' connector.",
        title=f"Enable connector '{name}'",
        proposed_change={"op": "enable", "name": name, "workspace_id": workspace_id},
        what="connector enable",
    )


async def _connector_disable_handler(args: dict) -> dict:
    """WRITE: disable a connector for the workspace. ADMIN-gated
    (``connector.manage``); Instinct-proposed. Carries op=disable + name."""
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — connector_disable can only be called from inside "
            "a cloud chat stream."
        )
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return _error_response("name is required (string — the connector to disable).")

    gate = await _gate_write(
        "connector_disable",
        _CONNECTOR_ACTION,
        "You don't have permission to manage connectors in this workspace",
    )
    if isinstance(gate, dict):
        return gate

    return await _propose_write(
        tool="connector_disable",
        action=_CONNECTOR_ACTION,
        workspace_id=workspace_id,
        user_id=user_id,
        args={"op": "disable", "name": name},
        summary=f"Disable the '{name}' connector.",
        title=f"Disable connector '{name}'",
        proposed_change={"op": "disable", "name": name, "workspace_id": workspace_id},
        what="connector disable",
    )


async def _connector_config_handler(args: dict) -> dict:
    """WRITE: patch a connector's saved config. ADMIN-gated (``connector.manage``);
    Instinct-proposed. ``config`` is a structured dict passed to the executor as
    OPAQUE data — the connectors service validates it and only merges it into the
    connector row's config (it never becomes top-level kwargs)."""
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — connector_config can only be called from inside "
            "a cloud chat stream."
        )
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return _error_response("name is required (string — the connector to configure).")
    config = args.get("config")
    if not isinstance(config, dict):
        return _error_response("config is required (object — the config patch to apply).")

    gate = await _gate_write(
        "connector_config",
        _CONNECTOR_ACTION,
        "You don't have permission to manage connectors in this workspace",
    )
    if isinstance(gate, dict):
        return gate

    return await _propose_write(
        tool="connector_config",
        action=_CONNECTOR_ACTION,
        workspace_id=workspace_id,
        user_id=user_id,
        args={"op": "config", "name": name, "config": config},
        summary=f"Update the '{name}' connector config.",
        title=f"Configure connector '{name}'",
        proposed_change={"op": "config", "name": name, "workspace_id": workspace_id},
        what="connector configuration",
    )


async def _workspace_update_handler(args: dict) -> dict:
    """WRITE: update workspace name / settings / branding. ADMIN-gated
    (``workspace.update``); Instinct-proposed. ONLY the recognized fields are
    proposed — any other key is dropped here (and again in the strict adapter)."""
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — workspace_update can only be called from inside "
            "a cloud chat stream."
        )

    name = args.get("name")
    settings = args.get("settings")
    branding = args.get("branding")
    if name is not None and not isinstance(name, str):
        return _error_response("name must be a string when provided.")
    if settings is not None and not isinstance(settings, dict):
        return _error_response("settings must be an object when provided.")
    if branding is not None and not isinstance(branding, dict):
        return _error_response("branding must be an object when provided.")

    # Build the STRICT proposed-args set from ONLY the recognized fields that were
    # actually supplied (an agent's stray keys never make it into the proposal).
    proposed_args: dict[str, Any] = {}
    if name is not None:
        proposed_args["name"] = name
    if settings is not None:
        proposed_args["settings"] = settings
    if branding is not None:
        proposed_args["branding"] = branding
    if not proposed_args:
        return _error_response(
            "provide at least one of name / settings / branding to update."
        )

    gate = await _gate_write(
        "workspace_update",
        _WORKSPACE_UPDATE_ACTION,
        "You don't have permission to update this workspace",
    )
    if isinstance(gate, dict):
        return gate

    return await _propose_write(
        tool="workspace_update",
        action=_WORKSPACE_UPDATE_ACTION,
        workspace_id=workspace_id,
        user_id=user_id,
        args=proposed_args,
        summary="Update workspace settings.",
        title="Update workspace settings",
        proposed_change={"fields": sorted(proposed_args.keys()), "workspace_id": workspace_id},
        what="workspace update",
    )


# ---------------------------------------------------------------------------
# WA-6 OWNER WRITE tool handlers — the most security-sensitive: destructive /
# financial / governance ops. Same propose-only shape as the ADMIN writes
# (validate → _gate_write deny-envelope → _propose_write pending) but gated on an
# OWNER RBAC action. The mutation / checkout fires ONLY on human approval, after
# the executor re-checks the proposer STILL holds OWNER. NONE mutates inline.
# ---------------------------------------------------------------------------


async def _instinct_approval_level_set_handler(args: dict) -> dict:
    """WRITE: set the workspace's Instinct-gate activation level. OWNER-gated;
    Instinct-proposed.

    A non-ASK level enables workspace-wide AUTO-APPROVAL of agent WRITE actions —
    the single most governance-sensitive switch in the gate — so this is OWNER-
    only AND still human-approved (the agent can't flip its own leash off). The
    ``level`` is constrained to the ApprovalLevel enum before any gate."""
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — instinct_approval_level_set can only be called "
            "from inside a cloud chat stream."
        )
    level = args.get("level")
    if not isinstance(level, str) or level not in _VALID_APPROVAL_LEVELS:
        return _error_response(
            f"level is required and must be one of {sorted(_VALID_APPROVAL_LEVELS)}."
        )

    gate = await _gate_write(
        "instinct_approval_level_set",
        _INSTINCT_ACTIVATE_ACTION,
        "You don't have permission to change this workspace's approval level",
    )
    if isinstance(gate, dict):
        return gate

    return await _propose_write(
        tool="instinct_approval_level_set",
        action=_INSTINCT_ACTIVATE_ACTION,
        workspace_id=workspace_id,
        user_id=user_id,
        args={"level": level},
        summary=f"Set the Instinct approval level to '{level}'.",
        title=f"Instinct approval level → {level}",
        proposed_change={"level": level, "workspace_id": workspace_id},
        what="Instinct approval-level change",
    )


async def _workspace_delete_handler(args: dict) -> dict:  # noqa: ARG001 — no args
    """WRITE: DELETE the entire workspace. OWNER-gated; Instinct-proposed.

    DESTRUCTIVE + IRREVERSIBLE — the service cascade strips the workspace from
    every member and (on the delete path) purges rooms / agents / files. This
    tool ONLY proposes it; the cascade fires on human approval. No args beyond
    identity. The envelope is emphatic about irreversibility."""
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — workspace_delete can only be called from inside "
            "a cloud chat stream."
        )

    gate = await _gate_write(
        "workspace_delete",
        _WORKSPACE_DELETE_ACTION,
        "You don't have permission to delete this workspace",
    )
    if isinstance(gate, dict):
        return gate

    # Bespoke pending envelope (not _propose_write) so the message can be emphatic
    # about the IRREVERSIBLE cascade — a generic "pending" line under-warns for a
    # workspace deletion.
    from pocketpaw_ee.cloud.admin_proposals.propose import propose_admin_action

    try:
        action_id = await propose_admin_action(
            workspace_id=workspace_id,
            action=_WORKSPACE_DELETE_ACTION,
            args={},  # identity only — nothing steers the delete
            proposer_user_id=user_id,
            summary="Delete the ENTIRE workspace (irreversible — cascades all members and data).",
            title="Delete workspace (IRREVERSIBLE)",
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean MCP error, never a 500
        logger.warning("workspace_delete: propose_admin_action failed", exc_info=True)
        return _error_response(f"could not file the workspace deletion for approval: {exc}")

    logger.info(
        "workspace_delete PROPOSED (WA-6): actor=%s workspace=%s proposal=%s — "
        "pending human approval in the Tray (IRREVERSIBLE)",
        user_id,
        workspace_id,
        action_id,
    )
    return _success_response(
        {
            "ok": True,
            "executed": False,
            "status": "pending_approval",
            "action_id": action_id,
            "proposal_id": action_id,
            "proposed_change": {"workspace_id": workspace_id, "irreversible": True},
            "message": (
                "DANGER: this permanently DELETES the entire workspace and cascades "
                "to EVERY member, room, agent, and file — it is IRREVERSIBLE and "
                "cannot be undone. It is an owner-level action and is NEVER applied "
                "directly from chat: I've proposed it and it is now PENDING approval "
                "in the Tray. NOTHING has been deleted yet, and nothing will be "
                "unless a human explicitly approves it. Tell the user you've "
                "requested the deletion, that it is irreversible, and that it needs "
                "a human to approve it — do NOT claim the workspace was deleted."
            ),
        }
    )


async def _billing_plan_change_handler(args: dict) -> dict:
    """WRITE: change the workspace's paid PLAN. OWNER-gated; Instinct-proposed.

    PAYMENT HONESTY: a paid-plan change flows through Dodo's HOSTED CHECKOUT — the
    plan flips ONLY when Dodo posts a verified ``subscription.active`` webhook, not
    synchronously. So on approval the executor does NOT fake a plan mutation: it
    calls ``billing.service.subscribe``, which returns a ``{checkout_url}`` a human
    must complete. The webhook-internal ``set_workspace_plan`` (which would bypass
    payment) is DELIBERATELY not wired. ``plan`` is constrained to the catalog."""
    workspace_id, user_id, _pocket_id = _identity()
    if not workspace_id or not user_id:
        return _error_response(
            "no active workspace — billing_plan_change can only be called from "
            "inside a cloud chat stream."
        )
    plan = args.get("plan")
    if not isinstance(plan, str) or plan not in _VALID_PLANS:
        return _error_response(
            f"plan is required and must be one of {sorted(_VALID_PLANS)}."
        )

    gate = await _gate_write(
        "billing_plan_change",
        _BILLING_MANAGE_ACTION,
        "You don't have permission to change this workspace's billing plan",
    )
    if isinstance(gate, dict):
        return gate

    # Bespoke pending envelope so the message can explain that approval PRODUCES a
    # checkout link the human must complete — the plan does NOT flip on approval
    # alone (it flips on the payment webhook), and the agent must relay that
    # honestly rather than claim the plan changed.
    from pocketpaw_ee.cloud.admin_proposals.propose import propose_admin_action

    try:
        action_id = await propose_admin_action(
            workspace_id=workspace_id,
            action=_BILLING_MANAGE_ACTION,
            args={"plan_key": plan},
            proposer_user_id=user_id,
            summary=f"Change the workspace plan to '{plan}' (opens a checkout on approval).",
            title=f"Billing plan change → {plan}",
        )
    except Exception as exc:  # noqa: BLE001 — surface a clean MCP error, never a 500
        logger.warning("billing_plan_change: propose_admin_action failed", exc_info=True)
        return _error_response(f"could not file the plan change for approval: {exc}")

    logger.info(
        "billing_plan_change PROPOSED (WA-6): actor=%s workspace=%s plan=%s "
        "proposal=%s — pending human approval; approval opens a Dodo checkout",
        user_id,
        workspace_id,
        plan,
        action_id,
    )
    return _success_response(
        {
            "ok": True,
            "executed": False,
            "status": "pending_approval",
            "action_id": action_id,
            "proposal_id": action_id,
            "proposed_change": {"plan": plan, "workspace_id": workspace_id},
            "message": (
                "Changing the paid plan is an owner-level action and is NEVER "
                "applied directly from chat. It also can't be flipped instantly: a "
                "paid plan changes only after checkout is completed and the payment "
                "provider confirms it. I've proposed this change; it's now PENDING "
                "approval in the Tray. When a human approves it, they'll get a "
                "secure CHECKOUT LINK to finish the change — the plan does NOT "
                "change until that checkout is completed. No plan change has been "
                "made yet. Tell the user you've requested it, that it needs approval "
                "and a checkout step — do NOT claim the plan was changed."
            ),
        }
    )


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def build_admin_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for workspace administration, or
    return ``None`` if the Claude Agent SDK isn't installed.

    Matches the ``(name, server)`` shape the other servers return so the
    backend's MCP registration loop in ``claude_sdk.py`` treats them uniformly.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_workspace_admin MCP disabled")
        return None

    @tool(
        "members_list",
        (
            "List the members of the CURRENT workspace — their email, name, "
            "workspace role (member / admin / owner), and when they joined. Call "
            "this when the user asks who is in the workspace, who the admins are, "
            "or to look up a member before changing their role. No arguments — "
            "the workspace is inferred from the active chat. Any workspace member "
            "may read the roster. If you don't have permission, the result says "
            "so (denied) — relay that; don't invent a member list."
        ),
        {},
    )
    async def members_list(args):  # type: ignore[no-untyped-def]
        return await _members_list_handler(args)

    @tool(
        "member_update_role",
        (
            "Propose changing a workspace member's ROLE (member / admin / owner). "
            "This is an ADMIN action and it is NEVER applied directly from chat — "
            "a role change is proposed for a human to approve and only takes "
            "effect once approved. Args: `user_id` (the member to change, from "
            "members_list) and `role` (the new role: 'member', 'admin', or "
            "'owner'). If you lack admin permission, the result says so (denied) "
            "— relay that. On success the result says the change is PENDING "
            "approval, not done — tell the user you've requested the change and "
            "it needs approval; do NOT claim the role was changed."
        ),
        {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "The member's user id (from members_list) to change.",
                },
                "role": {
                    "type": "string",
                    "enum": ["member", "admin", "owner"],
                    "description": "The new workspace role for the member.",
                },
            },
            "required": ["user_id", "role"],
            "additionalProperties": False,
        },
    )
    async def member_update_role(args):  # type: ignore[no-untyped-def]
        return await _member_update_role_handler(args)

    @tool(
        "workspace_settings_read",
        (
            "Read the CURRENT workspace's settings — its name, slug (URL handle), "
            "plan tier, seat usage (used / available), and any custom branding. "
            "Call this when the user asks about their workspace's plan, seat count, "
            "name, or branding. No arguments — the workspace is inferred from the "
            "active chat. Any workspace member may read this. If you don't have "
            "permission, the result says so (denied) — relay that."
        ),
        {},
    )
    async def workspace_settings_read(args):  # type: ignore[no-untyped-def]
        return await _workspace_settings_read_handler(args)

    @tool(
        "invites_list",
        (
            "List the CURRENT workspace's PENDING invites — the email invited, the "
            "role they'll get, who invited them, and when the invite expires. Call "
            "this when the user asks who has been invited or which invites are "
            "outstanding. No arguments. This is an ADMIN read — if you're not an "
            "admin, the result says so (denied); relay that, don't invent invites."
        ),
        {},
    )
    async def invites_list(args):  # type: ignore[no-untyped-def]
        return await _invites_list_handler(args)

    @tool(
        "connectors_list",
        (
            "List the connectors (integrations like Gmail, GitHub) available in the "
            "CURRENT workspace and whether each is enabled and connected. Call this "
            "when the user asks which integrations are set up or connected. No "
            "arguments — the workspace is inferred from the active chat, and the "
            "result is filtered to the connectors you're allowed to see. If you "
            "don't have permission, the result says so (denied) — relay that."
        ),
        {},
    )
    async def connectors_list(args):  # type: ignore[no-untyped-def]
        return await _connectors_list_handler(args)

    @tool(
        "billing_usage_read",
        (
            "Read the CURRENT workspace's usage and spend (daily credits and "
            "request counts, broken down by model) over an optional date window. "
            "Call this when the user asks how much they've spent or used. Optional "
            "args: `start` and `end` (YYYY-MM-DD); omit both for the last 30 days. "
            "Any workspace member may read their workspace's usage. If you don't "
            "have permission, the result says so (denied) — relay that."
        ),
        {
            "type": "object",
            "properties": {
                "start": {
                    "type": "string",
                    "description": "Window start, YYYY-MM-DD. Defaults to 30 days ago.",
                },
                "end": {
                    "type": "string",
                    "description": "Window end, YYYY-MM-DD. Defaults to today.",
                },
            },
            "additionalProperties": False,
        },
    )
    async def billing_usage_read(args):  # type: ignore[no-untyped-def]
        return await _billing_usage_read_handler(args)

    @tool(
        "audit_read",
        (
            "Read the CURRENT workspace's recent audit-log rows — who did what "
            "(action, actor, target) and when. Call this when the user asks what "
            "recently happened in the workspace or wants an activity/audit history. "
            "Optional arg: `limit` (1-100, default 50). This is an ADMIN read — if "
            "you're not an admin, the result says so (denied); relay that."
        ),
        {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": "How many recent rows to return (default 50).",
                },
            },
            "additionalProperties": False,
        },
    )
    async def audit_read(args):  # type: ignore[no-untyped-def]
        return await _audit_read_handler(args)

    @tool(
        "member_remove",
        (
            "Propose REMOVING a member from the CURRENT workspace. This is an ADMIN "
            "action and is NEVER applied directly from chat — it's proposed for a "
            "human to approve and only takes effect once approved. Removing a member "
            "also revokes their API keys and sessions and purges their personal "
            "connector data. Arg: `user_id` (the member to remove, from "
            "members_list). If you lack admin permission, the result says so "
            "(denied) — relay that. On success the result says the removal is "
            "PENDING approval, not done — do NOT claim the member was removed."
        ),
        {
            "type": "object",
            "properties": {
                "user_id": {
                    "type": "string",
                    "description": "The member's user id (from members_list) to remove.",
                },
            },
            "required": ["user_id"],
            "additionalProperties": False,
        },
    )
    async def member_remove(args):  # type: ignore[no-untyped-def]
        return await _member_remove_handler(args)

    @tool(
        "invite_create",
        (
            "Propose INVITING someone to the CURRENT workspace by email. This is an "
            "ADMIN action and is NEVER applied directly from chat — it's proposed "
            "for a human to approve. Args: `email` (who to invite) and `role` "
            "('admin' or 'member' — an invite can't be owner; defaults to 'member'). "
            "If you lack admin permission, the result says so (denied). On success "
            "the result says the invite is PENDING approval — do NOT claim it was "
            "sent."
        ),
        {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "The email address to invite.",
                },
                "role": {
                    "type": "string",
                    "enum": ["admin", "member"],
                    "description": "The role the invitee will get. Defaults to member.",
                },
            },
            "required": ["email"],
            "additionalProperties": False,
        },
    )
    async def invite_create(args):  # type: ignore[no-untyped-def]
        return await _invite_create_handler(args)

    @tool(
        "invite_revoke",
        (
            "Propose REVOKING a pending invite in the CURRENT workspace. This is an "
            "ADMIN action and is NEVER applied directly from chat — it's proposed "
            "for a human to approve. Arg: `invite_id` (from invites_list). If you "
            "lack admin permission, the result says so (denied). On success the "
            "result says the revocation is PENDING approval — do NOT claim it was "
            "revoked."
        ),
        {
            "type": "object",
            "properties": {
                "invite_id": {
                    "type": "string",
                    "description": "The invite id (from invites_list) to revoke.",
                },
            },
            "required": ["invite_id"],
            "additionalProperties": False,
        },
    )
    async def invite_revoke(args):  # type: ignore[no-untyped-def]
        return await _invite_revoke_handler(args)

    @tool(
        "connector_enable",
        (
            "Propose ENABLING a connector (integration like Gmail, GitHub) for the "
            "CURRENT workspace. This is an ADMIN action and is NEVER applied "
            "directly from chat — it's proposed for a human to approve. Arg: `name` "
            "(the connector name, from connectors_list). If you lack admin "
            "permission, the result says so (denied). On success the result says "
            "the change is PENDING approval — do NOT claim it was enabled."
        ),
        {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The connector name (from connectors_list) to enable.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    async def connector_enable(args):  # type: ignore[no-untyped-def]
        return await _connector_enable_handler(args)

    @tool(
        "connector_disable",
        (
            "Propose DISABLING a connector for the CURRENT workspace. This is an "
            "ADMIN action and is NEVER applied directly from chat — it's proposed "
            "for a human to approve. Arg: `name` (from connectors_list). If you lack "
            "admin permission, the result says so (denied). On success the result "
            "says the change is PENDING approval — do NOT claim it was disabled."
        ),
        {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The connector name (from connectors_list) to disable.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    )
    async def connector_disable(args):  # type: ignore[no-untyped-def]
        return await _connector_disable_handler(args)

    @tool(
        "connector_config",
        (
            "Propose UPDATING a connector's saved configuration in the CURRENT "
            "workspace. This is an ADMIN action and is NEVER applied directly from "
            "chat — it's proposed for a human to approve. Args: `name` (from "
            "connectors_list) and `config` (an object of config keys to patch — the "
            "connector validates it). If you lack admin permission, the result says "
            "so (denied). On success the result says the change is PENDING approval "
            "— do NOT claim it was applied."
        ),
        {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The connector name (from connectors_list) to configure.",
                },
                "config": {
                    "type": "object",
                    "description": "The config keys to patch (merged into the saved config).",
                },
            },
            "required": ["name", "config"],
            "additionalProperties": False,
        },
    )
    async def connector_config(args):  # type: ignore[no-untyped-def]
        return await _connector_config_handler(args)

    @tool(
        "workspace_update",
        (
            "Propose UPDATING the CURRENT workspace's name, settings, or branding. "
            "This is an ADMIN action and is NEVER applied directly from chat — it's "
            "proposed for a human to approve. Optional args: `name` (new workspace "
            "name), `settings` (a settings object), `branding` (a branding object); "
            "provide at least one. Only these fields are proposed — anything else is "
            "ignored. If you lack admin permission, the result says so (denied). On "
            "success the result says the change is PENDING approval — do NOT claim "
            "it was updated."
        ),
        {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "New workspace name.",
                },
                "settings": {
                    "type": "object",
                    "description": "Workspace settings object to apply.",
                },
                "branding": {
                    "type": "object",
                    "description": "Branding object (display_name, tab_title, accent_color, ...).",
                },
            },
            "additionalProperties": False,
        },
    )
    async def workspace_update(args):  # type: ignore[no-untyped-def]
        return await _workspace_update_handler(args)

    @tool(
        "instinct_approval_level_set",
        (
            "Propose changing the CURRENT workspace's Instinct APPROVAL LEVEL — how "
            "much agent write activity is auto-approved. This is an OWNER-level "
            "action and is NEVER applied directly from chat — it's proposed for a "
            "human to approve. A non-ASK level turns ON workspace-wide "
            "auto-approval of agent write actions, so it's highly sensitive. Arg: "
            "`level` — 'ASK' (every write goes to a human), 'TRIAGE', or 'TRUSTED'. "
            "If you lack owner permission, the result says so (denied) — relay that. "
            "On success the result says the change is PENDING approval — do NOT "
            "claim the level was changed."
        ),
        {
            "type": "object",
            "properties": {
                "level": {
                    "type": "string",
                    "enum": ["ASK", "TRIAGE", "TRUSTED"],
                    "description": "The Instinct-gate approval level to set.",
                },
            },
            "required": ["level"],
            "additionalProperties": False,
        },
    )
    async def instinct_approval_level_set(args):  # type: ignore[no-untyped-def]
        return await _instinct_approval_level_set_handler(args)

    @tool(
        "workspace_delete",
        (
            "Propose DELETING the ENTIRE current workspace. This is an OWNER-level, "
            "IRREVERSIBLE action — it permanently removes the workspace and every "
            "member, room, agent, and file in it, and it CANNOT be undone. It is "
            "NEVER applied directly from chat — it's proposed for a human to "
            "approve, and nothing is deleted unless a human explicitly approves it. "
            "No arguments — the workspace is inferred from the active chat. If you "
            "lack owner permission, the result says so (denied) — relay that. On "
            "success the result says the deletion is PENDING approval and is "
            "irreversible — warn the user clearly and do NOT claim the workspace "
            "was deleted."
        ),
        {},
    )
    async def workspace_delete(args):  # type: ignore[no-untyped-def]
        return await _workspace_delete_handler(args)

    @tool(
        "billing_plan_change",
        (
            "Propose changing the CURRENT workspace's paid PLAN. This is an "
            "OWNER-level action and is NEVER applied directly from chat — it's "
            "proposed for a human to approve. It also can't be flipped instantly: a "
            "paid plan changes only after a secure CHECKOUT is completed and the "
            "payment provider confirms it. When a human approves, they get a "
            "checkout link to finish the change. Arg: `plan` — one of 'free', 'go', "
            "'pro', 'pro_max', 'enterprise'. If you lack owner permission, the "
            "result says so (denied). On success the result says the change is "
            "PENDING approval and needs a checkout step — do NOT claim the plan was "
            "changed."
        ),
        {
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "enum": ["free", "go", "pro", "pro_max", "enterprise"],
                    "description": "The plan tier to change to.",
                },
            },
            "required": ["plan"],
            "additionalProperties": False,
        },
    )
    async def billing_plan_change(args):  # type: ignore[no-untyped-def]
        return await _billing_plan_change_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[
            members_list,
            member_update_role,
            workspace_settings_read,
            invites_list,
            connectors_list,
            billing_usage_read,
            audit_read,
            member_remove,
            invite_create,
            invite_revoke,
            connector_enable,
            connector_disable,
            connector_config,
            workspace_update,
            instinct_approval_level_set,
            workspace_delete,
            billing_plan_change,
        ],
    )
    return SERVER_NAME, server


__all__ = [
    "ADMIN_TOOL_IDS",
    "AUDIT_READ_TOOL_ID",
    "BILLING_PLAN_CHANGE_TOOL_ID",
    "BILLING_USAGE_READ_TOOL_ID",
    "CONNECTORS_LIST_TOOL_ID",
    "CONNECTOR_CONFIG_TOOL_ID",
    "CONNECTOR_DISABLE_TOOL_ID",
    "CONNECTOR_ENABLE_TOOL_ID",
    "INSTINCT_APPROVAL_LEVEL_SET_TOOL_ID",
    "INVITES_LIST_TOOL_ID",
    "INVITE_CREATE_TOOL_ID",
    "INVITE_REVOKE_TOOL_ID",
    "MEMBERS_LIST_TOOL_ID",
    "MEMBER_REMOVE_TOOL_ID",
    "MEMBER_UPDATE_ROLE_TOOL_ID",
    "SERVER_NAME",
    "WORKSPACE_DELETE_TOOL_ID",
    "WORKSPACE_SETTINGS_READ_TOOL_ID",
    "WORKSPACE_UPDATE_TOOL_ID",
    "build_admin_server",
]
