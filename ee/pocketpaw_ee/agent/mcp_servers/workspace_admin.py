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

ADMIN_TOOL_IDS = (
    MEMBERS_LIST_TOOL_ID,
    MEMBER_UPDATE_ROLE_TOOL_ID,
    WORKSPACE_SETTINGS_READ_TOOL_ID,
    INVITES_LIST_TOOL_ID,
    CONNECTORS_LIST_TOOL_ID,
    BILLING_USAGE_READ_TOOL_ID,
    AUDIT_READ_TOOL_ID,
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

# The workspace roles a member may be assigned via member_update_role. Kept in
# sync with ``guards.rbac.WorkspaceRole`` — an out-of-set value is refused
# before any gate check (defense in depth; the tool schema also constrains it).
_VALID_ROLES = frozenset({"member", "admin", "owner"})


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
        ],
    )
    return SERVER_NAME, server


__all__ = [
    "ADMIN_TOOL_IDS",
    "AUDIT_READ_TOOL_ID",
    "BILLING_USAGE_READ_TOOL_ID",
    "CONNECTORS_LIST_TOOL_ID",
    "INVITES_LIST_TOOL_ID",
    "MEMBERS_LIST_TOOL_ID",
    "MEMBER_UPDATE_ROLE_TOOL_ID",
    "SERVER_NAME",
    "WORKSPACE_SETTINGS_READ_TOOL_ID",
    "build_admin_server",
]
