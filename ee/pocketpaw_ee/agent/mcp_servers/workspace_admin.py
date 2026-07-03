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
#   OSS-EE boundary: this module imports only ``workspace.service`` +
#   ``guards.deps`` + the User Beanie load, all lazily inside handlers, so the
#   import surface stays minimal (mirrors connectors.py's function-local imports).
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

ADMIN_TOOL_IDS = (
    MEMBERS_LIST_TOOL_ID,
    MEMBER_UPDATE_ROLE_TOOL_ID,
)

# The RBAC action keys these tools gate on (canonical entries in
# ``guards.actions.ACTIONS``). ``workspace.view`` = MEMBER (read the roster),
# ``workspace.member.role_change`` = ADMIN (change a member's role).
_READ_ACTION = "workspace.view"
_ROLE_CHANGE_ACTION = "workspace.member.role_change"

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

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[members_list, member_update_role],
    )
    return SERVER_NAME, server


__all__ = [
    "ADMIN_TOOL_IDS",
    "MEMBERS_LIST_TOOL_ID",
    "MEMBER_UPDATE_ROLE_TOOL_ID",
    "SERVER_NAME",
    "build_admin_server",
]
