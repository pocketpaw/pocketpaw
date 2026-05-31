# RBAC audit helpers — thin wrappers over pocketpaw.security.audit that
# emit structured events for authorization denials and privileged actions.

from __future__ import annotations

from typing import Any

from pocketpaw.security.audit import AuditEvent, AuditSeverity, get_audit_logger


def log_denial(
    *,
    actor: str,
    action: str,
    code: str,
    resource_id: str | None = None,
    workspace_id: str | None = None,
    detail: str = "",
    **extra: Any,
) -> None:
    """Record an authorization denial.

    `actor` is the user id; `action` matches an entry in ACTIONS; `code` is
    the machine-readable denial reason that the frontend keys off of.
    """
    context: dict[str, Any] = {"code": code, "detail": detail}
    if workspace_id is not None:
        context["workspace_id"] = workspace_id
    context.update(extra)
    get_audit_logger().log(
        AuditEvent.create(
            severity=AuditSeverity.ALERT,
            actor=actor,
            action=f"rbac.deny:{action}",
            target=resource_id or "",
            status="block",
            **context,
        )
    )


def log_privileged_action(
    *,
    actor: str,
    action: str,
    resource_id: str | None = None,
    workspace_id: str | None = None,
    status: str = "success",
    **extra: Any,
) -> None:
    """Record a successful privileged action (role change, invite, billing,
    workspace delete, ownership transfer, etc.).
    """
    context: dict[str, Any] = {}
    if workspace_id is not None:
        context["workspace_id"] = workspace_id
    context.update(extra)
    get_audit_logger().log(
        AuditEvent.create(
            severity=AuditSeverity.CRITICAL,
            actor=actor,
            action=f"rbac.privileged:{action}",
            target=resource_id or "",
            status=status,
            **context,
        )
    )
