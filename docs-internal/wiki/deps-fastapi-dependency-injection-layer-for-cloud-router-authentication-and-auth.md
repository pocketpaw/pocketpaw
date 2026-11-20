# deps — FastAPI dependency injection layer for cloud router authentication and authorization

> This module provides FastAPI dependency functions that extract and validate user authentication and workspace context from JWT tokens. It exists to centralize credential handling and role-based access control across cloud routers, eliminating repeated auth logic and ensuring consistent security checks. It serves as the bridge between FastAPI's dependency injection system and the application's authentication/authorization model.

**Categories:** Authentication & Authorization, API Router Layer, FastAPI Middleware & Dependency Injection, Multi-Tenant Access Control  
**Concepts:** FastAPI dependency injection, JWT authentication, Role-based access control (RBAC), Workspace isolation, current_active_user, current_user, current_user_id, current_workspace_id, optional_workspace_id, require_role  
**Words:** 1715 | **Version:** 1

---

## Purpose

The `deps` module solves a critical architectural problem: **how to reliably inject authenticated user context and workspace scope into every cloud router endpoint without duplicating code**.

In a multi-tenant cloud application, nearly every API endpoint needs to:
1. Verify the request comes from an authenticated user (via JWT token)
2. Extract the user's active workspace context
3. Optionally validate the user has a minimum role in that workspace

Instead of repeating these checks in every endpoint handler, FastAPI provides dependency injection. This module wraps authentication logic into reusable dependency functions that FastAPI automatically invokes and injects.

**System Position**: This module sits at the intersection of three concerns:
- **Authentication layer** (`ee.cloud.auth.current_active_user`): Provides the raw authenticated User object from the JWT token
- **Authorization layer** (`ee.cloud.shared.permissions.check_workspace_role`): Validates role requirements
- **Router layer** (consuming modules like `__init__`, `router`): Uses these dependencies as function parameters in endpoint handlers

## Key Classes and Methods

This module contains only functions, no classes. Each function is a FastAPI dependency that can be injected into endpoint handlers.

### `current_user(user: User) → User`
**What it does**: Returns the authenticated user object.

**Why it exists**: Provides a named, documented dependency that makes endpoints' authentication requirements explicit. Endpoints that need just the user object (not workspace-scoped operations) depend on this.

**How it works**: It declares a dependency on `current_active_user` (from the auth module), which handles the actual JWT validation. This function simply passes it through, creating a semantic checkpoint.

### `current_user_id(user: User) → str`
**What it does**: Extracts and returns the authenticated user's ID as a string.

**Why it exists**: Some endpoints need only the user ID, not the full user object. This provides that without forcing callers to extract it themselves. Also normalizes the ID to string type.

**How it works**: Depends on `current_active_user`, then converts `user.id` to a string. The conversion is important because IDs might be integers or other types in the User model, but APIs prefer string representations.

### `current_workspace_id(user: User) → str`
**What it does**: Returns the user's currently active workspace ID, or raises an HTTP 400 error if none is set.

**Critical behavior**: This dependency has a **hard requirement**—it enforces that the user must have an active workspace. This is the primary validation point for workspace-scoped operations.

**How it works**: 
1. Depends on `current_active_user` to get the user
2. Checks `user.active_workspace` is not None/empty
3. If missing, raises `HTTPException(400)` with a user-friendly message: "No active workspace. Create or join a workspace first."
4. If present, returns the workspace ID

**Edge case**: The error message guides users toward resolving the issue (create or join a workspace), suggesting this is a common problem in the UX.

### `optional_workspace_id(user: User) → str | None`
**What it does**: Returns the user's active workspace ID if set, or None if not.

**Key difference from `current_workspace_id`**: This is **permissive**—it allows endpoints to work even if the user has no active workspace.

**Use case**: Endpoints that don't inherently require a workspace context (e.g., "list all my workspaces," "create a new workspace") should use this. Workspace-scoped operations like "read workspace files" should use the stricter `current_workspace_id`.

**How it works**: Simply returns `user.active_workspace` directly, which FastAPI converts to None if absent.

### `require_role(minimum: str) → async callable`
**What it does**: A dependency factory that returns a new dependency function enforcing minimum workspace role requirements.

**Why it exists**: Implements **role-based access control (RBAC)** at the dependency layer. It lets endpoints declare "only admins can do this" or "editors and above are allowed" without embedding role logic in handler code.

**How it works** (closure pattern):
1. `require_role("admin")` is called, returning an inner `_check` function
2. The inner function depends on both `current_active_user` (to get the user) and `current_workspace_id` (to know which workspace to check permissions for)
3. Inside `_check`:
   - It finds the user's workspace membership record by matching `w.workspace == workspace_id`
   - If no membership is found, raises `Forbidden` (403) with code "workspace.not_member"
   - If found, calls `check_workspace_role(membership.role, minimum=minimum)` to validate the user's role meets the minimum
   - The role check will raise `Forbidden` if the role is insufficient
   - If all checks pass, returns the user

**Membership lookup**: The line `next((w for w in user.workspaces if w.workspace == workspace_id), None)` iterates through the user's workspace memberships until it finds one matching the current workspace.

**Example usage in a router**:
```python
@router.delete("/workspaces/{workspace_id}/files/{file_id}")
async def delete_file(
    file_id: str,
    user: User = Depends(require_role("admin"))
):
    # At this point, user is guaranteed to be an admin in the current workspace
    # FastAPI has already executed the role check dependency
    pass
```

## How It Works

### Data Flow

**Request arrives at an endpoint**:
1. The endpoint declares a dependency, e.g., `user: User = Depends(current_user)`
2. FastAPI's dependency injection system sees this and calls `current_user()`
3. `current_user()` declares its own dependency: `user: User = Depends(current_active_user)`
4. FastAPI calls `current_active_user()` (from the auth module), which validates the JWT token and returns a User object or raises an exception
5. The User object is passed to `current_user()`, which returns it
6. The endpoint handler receives the User object and executes

**For workspace-scoped operations**:
1. Endpoint depends on `current_workspace_id`
2. `current_workspace_id` depends on `current_active_user`
3. FastAPI caches the User object (doesn't call `current_active_user` twice)
4. `current_workspace_id` extracts and validates the workspace ID
5. Endpoint receives the workspace ID

**For role-based operations**:
1. Endpoint depends on `require_role("admin")`
2. This returns the inner `_check` function
3. FastAPI injects `current_active_user` and `current_workspace_id` into `_check`
4. `_check` validates the user is a member and has the required role
5. Endpoint receives the authenticated, authorized user

### Dependency Caching

FastAPI caches dependency results within a single request. If both `current_user_id` and `current_workspace_id` are used in the same endpoint, `current_active_user` is called only once, and the User object is reused. This is efficient.

### Error Handling

- **No authentication**: `current_active_user` (from auth module) raises an exception if the JWT is invalid or missing
- **No active workspace** (when required): `current_workspace_id` raises `HTTPException(400)`
- **Not a workspace member**: `require_role` raises `Forbidden` with code "workspace.not_member"
- **Insufficient role**: `check_workspace_role` raises `Forbidden` with a message indicating what role is required

## Authorization and Security

### Authentication

This module assumes authentication has already been done by `current_active_user` (imported from `ee.cloud.auth`). That function validates JWT tokens. This module **does not** handle token validation—it only consumes authenticated users.

### Authorization (Access Control)

This module implements two layers of authorization:

**1. Workspace membership check** (`require_role`):
- Only users who are members of a workspace can perform workspace-scoped actions
- A user may be a member of multiple workspaces; we check membership in the *active* workspace

**2. Role-based access control** (`check_workspace_role`):
- Within a workspace, users have roles (e.g., "admin", "editor", "viewer")
- Endpoints declare a minimum role requirement
- Only users with a role at or above that level can proceed

### Workspace Isolation

These dependencies enforce **strict workspace isolation**:
- `current_workspace_id` always returns the user's *active* workspace
- Endpoints cannot opt into a different workspace
- If a user switches their active workspace (in the User model), all subsequent requests operate in that workspace

This prevents accidental cross-workspace data access.

## Dependencies and Integration

### What This Module Depends On

1. **`ee.cloud.auth.current_active_user`**
   - **What**: The actual authentication function that validates JWT tokens
   - **Why**: This module only handles post-authentication concerns (context extraction, role checks). The heavy lifting of token validation is delegated to the auth module.

2. **`ee.cloud.models.user.User`**
   - **What**: The User data model
   - **Why**: All dependencies work with User objects. The User model contains `active_workspace` and `workspaces` attributes.

3. **`ee.cloud.shared.errors.Forbidden`**
   - **What**: A custom exception class for authorization failures
   - **Why**: Provides a consistent, application-specific way to signal 403 Forbidden errors instead of generic FastAPI HTTPException.

4. **`ee.cloud.shared.permissions.check_workspace_role`**
   - **What**: A role validation function
   - **Why**: Centralizes the logic for comparing a user's role against a minimum requirement. This module calls it but doesn't implement role comparison itself.

### What Depends On This Module

1. **`__init__` (the package init)**
   - Likely re-exports these dependencies so other modules can import them as `from ee.cloud.shared import current_user, require_role`, etc.

2. **`router` (cloud routers)**
   - Cloud API endpoints use these dependencies in their handler signatures
   - Example: `async def create_file(file: FileCreate, user: User = Depends(current_user), workspace_id: str = Depends(current_workspace_id))`

### System Architecture Position

```
Request
   ↓
[FastAPI Router]
   ↓
[Endpoint Handler]
   ↓
[deps.py - Dependency Injection]
   ├→ current_user
   ├→ current_workspace_id (validation)
   └→ require_role (RBAC)
   ↓
[Auth Module - JWT Validation]
   ↓
[Permissions Module - Role Checking]
   ↓
[Handler Executes with Validated Context]
```

## Design Decisions

### 1. **Dependency Injection Over Middleware**

Why not validate in middleware? Because:
- Dependencies are **endpoint-specific**. Different endpoints need different validation (some require workspace, others don't). Middleware would validate the same way for all routes.
- Dependencies are **composable**. `require_role` accepts a parameter, allowing fine-grained control per endpoint.
- Dependencies integrate with **FastAPI's automatic documentation** (OpenAPI). They show up in generated API docs.

### 2. **Separate Functions for Different Extraction Needs**

Why have `current_user`, `current_user_id`, `current_workspace_id`, and `optional_workspace_id` instead of a single function?
- **Precision**: Endpoints declare exactly what they need. If an endpoint only needs the workspace ID, it doesn't pay the cost of loading the full user object (though in practice this is often cached).
- **Clarity**: Code is self-documenting. `Depends(current_workspace_id)` clearly indicates the endpoint requires an active workspace.
- **Validation**: `current_workspace_id` enforces the workspace requirement; `optional_workspace_id` doesn't. This prevents bugs where an endpoint accidentally allows requests without a workspace.

### 3. **Closure Pattern for `require_role`**

Why return a function instead of being a direct dependency?
- **Parameterization**: The role requirement varies per endpoint ("admin" vs. "editor" vs. "viewer"). A closure captures the `minimum` parameter.
- **Clean API**: Endpoints write `Depends(require_role("admin"))`, which reads naturally.

### 4. **Explicit Error Messages**

- `current_workspace_id` raises `HTTPException(400, "No active workspace. Create or join a workspace first.")` instead of a generic 400, guiding users toward a fix.
- `require_role` raises `Forbidden("workspace.not_member", ...)` with a machine-readable code, allowing frontends to handle specific error types.

These choices reflect the principle that **security errors should guide users to compliance**, not just reject requests.

### 5. **No Business Logic**

This module intentionally contains **only routing and validation logic**, not business logic:
- It doesn't modify users or workspaces
- It doesn't query databases
- It delegates role comparison to the permissions module

This keeps dependencies lightweight and testable.

---

## Related

- [untitled](untitled.md)
- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
