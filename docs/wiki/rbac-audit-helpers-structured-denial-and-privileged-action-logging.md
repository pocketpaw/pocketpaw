---
{
  "title": "RBAC Audit Helpers: Structured Denial and Privileged Action Logging",
  "summary": "The `audit.py` module provides two thin wrapper functions — `log_denial` and `log_privileged_action` — that emit structured `AuditEvent` records whenever authorization decisions block or allow a sensitive operation. These wrappers ensure that RBAC events are logged at the correct severity level with machine-readable codes that can be queried, alerted on, or exported to a SIEM.",
  "concepts": [
    "audit logging",
    "log_denial",
    "log_privileged_action",
    "AuditEvent",
    "AuditSeverity",
    "RBAC audit trail",
    "machine-readable codes",
    "security monitoring",
    "structured logging"
  ],
  "categories": [
    "security",
    "enterprise edition",
    "audit",
    "authorization"
  ],
  "source_docs": [
    "cf4bd0b33c7900ac"
  ],
  "backlinks": null,
  "word_count": 446,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`audit.py` (`src/pocketpaw/ee/guards/audit.py`) sits between the guard decision logic and the underlying security audit infrastructure. It translates RBAC outcomes into structured log entries by wrapping `pocketpaw.security.audit.AuditEvent.create()`, adding RBAC-specific fields and action name formatting.

## log_denial

```python
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
```

This function is called every time `check_workspace_action` or `check_group_action` raises a `Forbidden`. The parameters map directly to structured audit fields:

- `actor`: the user ID attempting the action
- `action`: the dotted action string (e.g., `"workspace.delete"`), which matches an entry in `ACTIONS`
- `code`: the machine-readable denial reason (e.g., `"workspace.insufficient_role"`), which the frontend uses to display localized error messages
- `resource_id`: the specific resource being acted on (pocket ID, group ID, etc.)
- `workspace_id`: included in the event context when available for cross-workspace correlation

The `**extra` kwargs allow callers to attach arbitrary metadata (e.g., the attempted target role) without changing the function signature.

The event is logged with `AuditSeverity.ALERT` — one step below CRITICAL. This severity choice reflects that a denial is a security-relevant event worth monitoring for patterns (e.g., repeated denials from one actor) but is not by itself a confirmed breach.

The action field on the emitted `AuditEvent` is formatted as `f"rbac.deny:{action}"` — for example, `"rbac.deny:workspace.delete"`. This prefix convention makes RBAC events trivially filterable in audit log queries without needing to inspect the event body.

## log_privileged_action

```python
def log_privileged_action(
    *,
    actor: str,
    action: str,
    resource_id: str | None = None,
    workspace_id: str | None = None,
    status: str = "success",
    **extra: Any,
) -> None:
```

This is the success-path audit companion to `log_denial`. It is called after high-stakes operations like role changes, ownership transfers, workspace deletion, or billing management complete successfully. Unlike `log_denial`, it uses `AuditSeverity.CRITICAL`, reflecting that a successful privileged action is more operationally significant than a blocked attempt.

Action names are formatted as `"rbac.privileged:{action}"`, e.g., `"rbac.privileged:workspace.transfer"`.

## Why a Dedicated Wrapper?

The wrapper pattern exists for two reasons. First, it enforces consistent field naming: callers don't need to remember that RBAC denials should use severity ALERT and prefix `rbac.deny:`. Second, it insulates the guard layer from changes to the audit infrastructure — if `AuditEvent.create()` gains new required parameters, only this file needs updating.

## Known Gaps

- Neither function is async. If `get_audit_logger().log()` ever becomes an async operation (e.g., to write to a remote SIEM endpoint), these wrappers would need to be refactored to `async def`.
- The `**extra` pattern makes it easy to pass sensitive data accidentally (e.g., raw API keys in error context) without any sanitization at the call site.