---
{
  "title": "FastAPI Dependency Injection for Cloud Routers",
  "summary": "This module provides reusable FastAPI dependency functions that extract identity and workspace context from authenticated requests, and composable action-based RBAC guards. It is the single source of truth for authentication wiring across all cloud EE router endpoints.",
  "concepts": [
    "FastAPI Depends",
    "dependency injection",
    "JWT authentication",
    "RBAC",
    "active_workspace",
    "current_active_user",
    "action guards",
    "audit logging",
    "Forbidden error",
    "workspace scoping"
  ],
  "categories": [
    "authentication",
    "authorization",
    "FastAPI",
    "cloud EE"
  ],
  "source_docs": [
    "c3326825dbe48824"
  ],
  "backlinks": null,
  "word_count": 463,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/shared/deps.py` centralizes all FastAPI `Depends` callables that cloud routers need to extract authentication context and enforce access control. Rather than each router reimplementing JWT parsing or workspace lookup, routers declare these as parameter dependencies and FastAPI injects the resolved values.

## Identity Dependencies

Three functions cover the most common identity extraction patterns:

- `current_user` — returns the full `User` document after JWT validation via `current_active_user`
- `current_user_id` — extracts just the string user ID, avoiding a full model round-trip in endpoints that only need the ID
- `current_workspace_id` — extracts the user's `active_workspace` and raises `HTTP 400` if none is set

The `current_workspace_id` guard is important: without it, an endpoint that silently accepts a `None` workspace would create data in an unscoped namespace, making it readable by any authenticated user.

## Optional Workspace

`optional_workspace_id` returns `str | None` for endpoints that work both inside and outside a workspace context, such as discovery or onboarding flows that run before a workspace is created.

## Action-Based RBAC Guards

The module exposes `require_action`, a factory that returns a FastAPI dependency checking whether the authenticated user is allowed to perform a named action within a workspace. This is the canonical pattern established on 2026-04-14:

```python
def require_action(
    action: str,
    workspace_dep: _WorkspaceIdDep = _workspace_id_from_path,
) -> ...:
    ...
```

The `workspace_dep` parameter allows callers to source the workspace ID from the path (default), from the authenticated user's active workspace, or from a query parameter — whichever makes sense for the endpoint's URL shape.

## Why Not Inline the Checks

Without a shared dependency layer, every router would independently call `current_active_user`, extract the workspace, and call RBAC checks. This pattern spreads auth logic across dozens of files and makes it impossible to enforce consistently. A single breaking change in JWT structure or workspace lookup would require updating every router individually.

## Denial Logging

Denied access attempts are forwarded to `log_denial` from `pocketpaw.ee.guards.audit`. This ensures that all access refusals are captured in the audit log regardless of which endpoint triggered them — the logging is not opt-in per-endpoint.

## Error Normalization

The module imports `Forbidden` from two sources: `ee.cloud.shared.errors` (the cloud domain's error type) and `pocketpaw.ee.guards.rbac` (the guard layer's type). This dual import suggests the guard layer may raise its own `Forbidden` type that needs to be caught and re-raised as the cloud `Forbidden`. Callers relying on a single `Forbidden` type should be aware both exist.

## Known Gaps

- `_workspace_id_from_path` assumes the path parameter is named exactly `workspace_id`. Endpoints that use a different parameter name must pass a custom `workspace_dep` factory, which is not documented at the call site.
- There is no dependency for extracting workspace ID from a request body — multi-resource endpoints that embed workspace context in JSON payloads must handle that extraction themselves.