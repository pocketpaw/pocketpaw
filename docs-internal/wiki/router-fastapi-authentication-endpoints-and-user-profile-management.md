# router — FastAPI authentication endpoints and user profile management

> This module exposes HTTP endpoints for user authentication, registration, profile retrieval, profile updates, and workspace selection. It acts as the HTTP layer for the auth domain, delegating business logic to AuthService while leveraging fastapi-users for standardized OAuth2/cookie-based authentication. It exists as a separate module to cleanly separate API route definitions from domain logic and security policies.

**Categories:** authentication, API router layer, FastAPI HTTP endpoints, CRUD operations, multi-tenant architecture  
**Concepts:** APIRouter, FastAPI dependency injection, Depends(current_active_user), fastapi-users library, cookie-based authentication, bearer token authentication, OAuth2, stateless authentication, user profile management, workspace scoping  
**Words:** 1431 | **Version:** 1

---

## Purpose

The router module is the **HTTP API layer** for the authentication domain in PocketPaw's cloud infrastructure. It serves three critical functions:

1. **Expose authentication endpoints**: Provides login, logout, and user registration routes via fastapi-users integration
2. **User profile management**: Allows authenticated users to retrieve and update their profiles
3. **Workspace routing**: Enables users to select their active workspace, a core feature of multi-tenant applications

This module exists because PocketPaw separates concerns into layers:
- **Core** (`ee.cloud.auth.core`): Authentication configuration and security setup
- **Service** (`ee.cloud.auth.service`): Business logic for profile and workspace operations
- **Router** (this module): HTTP endpoint definitions that bind requests to service calls

This layered architecture makes the codebase testable, maintainable, and allows non-HTTP interfaces (e.g., gRPC, webhooks) to reuse the same service logic.

## Key Classes and Methods

### Router Instance
```python
router = APIRouter(tags=["Auth"])
```
A FastAPI APIRouter instance tagged as "Auth" for OpenAPI documentation. All endpoints in this module are registered here and later included in the main FastAPI application via the `__init__.py` module.

### Included Routers (from fastapi-users)

The module includes three pre-built router sets from the fastapi-users library:

1. **Cookie-based authentication** (`/auth` prefix)
   - Endpoints: POST `/auth/login`, POST `/auth/logout`
   - Uses HTTP cookies for session management
   - Ideal for browser-based clients

2. **Bearer token authentication** (`/auth/bearer` prefix)
   - Endpoints: POST `/auth/bearer/login`, POST `/auth/bearer/logout`
   - Uses Authorization header with JWT/bearer tokens
   - Ideal for API clients, mobile apps, third-party integrations

3. **User registration** (`/auth` prefix)
   - Endpoint: POST `/auth/register`
   - Creates new User records with UserCreate schema validation
   - Returns UserRead schema on success

These are **framework-provided routes** that handle the heavy lifting of OAuth2/OpenID flows, password hashing, and token management.

### `get_me(user)` → GET `/auth/me`
**Purpose**: Return the authenticated user's profile information.

**Parameters**:
- `user`: Injected via `Depends(current_active_user)` — a User dependency that verifies the request includes valid authentication credentials

**Implementation**: Delegates to `AuthService.get_profile(user)`, which formats the user's core profile data (likely ID, email, name, workspace associations) for the response.

**Security**: Only accessible with valid authentication. The `current_active_user` dependency (from `ee.cloud.auth.core`) enforces this.

**Use case**: Called by frontend when loading user dashboard or sidebar to display "Logged in as [name]".

### `update_me(body, user)` → PATCH `/auth/me`
**Purpose**: Allow authenticated users to update their own profile information.

**Parameters**:
- `body`: A `ProfileUpdateRequest` schema object containing fields the user wants to update (e.g., name, avatar, preferences)
- `user`: The authenticated user making the request (dependency injection)

**Implementation**: Passes both to `AuthService.update_profile(user, body)`, which validates changes, applies updates, and persists to the database.

**Security**: Only the user's own profile can be updated (enforced by receiving their own User object from the dependency).

**Use case**: Allows users to change their name, profile picture, or other mutable user attributes.

### `set_active_workspace(body, user)` → POST `/auth/set-active-workspace`
**Purpose**: Update which workspace the user is currently working in (for multi-tenant workspaces).

**Parameters**:
- `body`: A `SetWorkspaceRequest` containing the `workspace_id` to activate
- `user`: The authenticated user

**Implementation**: 
1. Calls `AuthService.set_active_workspace(user, body.workspace_id)` to update the user's active workspace
2. Returns a confirmation response with the format: `{"ok": True, "activeWorkspace": "workspace-id"}`

**Security**: The service layer likely verifies the user has access to the requested workspace (preventing privilege escalation).

**Use case**: When a user with access to multiple workspaces switches between them (e.g., "Switch to Workspace B").

## How It Works

### Request Flow

1. **HTTP Request arrives** at an endpoint (e.g., `GET /auth/me`)
2. **FastAPI processes** route matching and dependency injection
3. **`current_active_user` dependency** (from `ee.cloud.auth.core`) validates authentication:
   - Checks for valid cookie or bearer token
   - Extracts the User object from the session/token
   - Raises 401 Unauthorized if missing or invalid
4. **Endpoint handler** receives the validated `user` and/or `body`
5. **Delegates to AuthService** methods to perform business logic (retrieve profiles, validate workspace access, etc.)
6. **Response returned** to client as JSON

### Data Flow for Profile Update

```
Client Request (PATCH /auth/me with ProfileUpdateRequest)
    ↓
FastAPI validates ProfileUpdateRequest against schema
    ↓
Dependency injection: current_active_user verifies auth
    ↓
update_me() calls AuthService.update_profile(user, body)
    ↓
AuthService applies business logic (validation, db updates)
    ↓
Response returned to client
```

### Data Flow for Workspace Switch

```
Client Request (POST /auth/set-active-workspace with workspace_id)
    ↓
Dependency injection: current_active_user retrieves User
    ↓
set_active_workspace() calls AuthService.set_active_workspace()
    ↓
AuthService validates user has access to workspace
    ↓
AuthService updates user.active_workspace in database
    ↓
Confirmation response sent
```

### Key Design: Dependency Injection

FastAPI's dependency injection system (`Depends()`) is used to:
- **Enforce authentication** before the endpoint handler runs
- **Reduce boilerplate** (no manual token parsing in each endpoint)
- **Improve testability** (dependencies can be mocked in unit tests)
- **Centralize security logic** (auth rules live in one place: `current_active_user`)

## Authorization and Security

### Authentication Methods
Two parallel mechanisms support different client types:

1. **Cookie-based** (browsers): Stateful sessions, CSRF-protected
2. **Bearer tokens** (API clients): Stateless JWT/OAuth2 tokens

Both use the same underlying User model and validation logic.

### Access Control

**Profile endpoints** (`/auth/me`, `PATCH /auth/me`):
- Require valid authentication (enforced by `current_active_user` dependency)
- Allow users to read/modify only their own profile (implicit — the dependency provides their own User object)

**Workspace endpoint** (`/auth/set-active-workspace`):
- Requires valid authentication
- Likely requires the user to be a member of the target workspace (validation happens in AuthService, not this router)
- Prevents privilege escalation: user cannot switch to a workspace they don't have access to

### Security Best Practices Evident

- **No direct database access** in endpoints: all logic in service layer
- **Input validation** via Pydantic schemas (ProfileUpdateRequest, SetWorkspaceRequest)
- **Dependency injection for auth**: cannot accidentally call endpoints without auth checks
- **Password hashing** delegated to fastapi-users (not visible here but used during registration/login)

## Dependencies and Integration

### What This Module Imports

| Import | Purpose |
|--------|----------|
| `fastapi.APIRouter, Depends` | Core FastAPI routing and dependency injection |
| `ee.cloud.auth.core` | Provides `fastapi_users`, auth backends (cookie, bearer), `current_active_user`, schema models (UserRead, UserCreate) |
| `ee.cloud.auth.schemas` | Request/response models: ProfileUpdateRequest, SetWorkspaceRequest |
| `ee.cloud.auth.service` | AuthService class with profile and workspace business logic |
| `ee.cloud.models.user` | User ORM model |

**Note on unused imports**: The module imports from many other ee.cloud domains (license, knowledge, user, ws, group, message, errors, backend_adapter, workspace) but doesn't directly use them. These likely come from the `__init__.py` which might re-export this router alongside other domain routers.

### What Imports This Module

| Importer | Usage |
|----------|-------|
| `ee.cloud.auth.__init__` | Includes `router` in the auth domain's public API |
| Root FastAPI application | Includes this router to expose `/auth/*` endpoints |

### Integration Points

1. **AuthService** (`ee.cloud.auth.service`): Handles all business logic for profile/workspace operations
2. **Authentication Core** (`ee.cloud.auth.core`): Configures FastAPI-users, manages backends
3. **User Model** (`ee.cloud.models.user`): Represents authenticated users and their workspace memberships
4. **Workspace domain**: The `set_active_workspace` endpoint connects to workspace management (user selects which workspace to work in)

## Design Decisions

### 1. **Thin Router, Thick Service Layer**
The router endpoints are intentionally minimal — they accept parameters, delegate to AuthService, and return responses. Business logic (validation, database updates) lives in the service layer. This makes it easy to:
- Add new HTTP transports (gRPC, webhooks) without duplicating logic
- Test business logic independently of HTTP details
- Change HTTP contracts without rewriting core logic

### 2. **Separate Authentication Backends**
Offering both cookie and bearer token routes acknowledges different client needs:
- Browsers use cookies (simpler, less config, CSRF-protected by convention)
- APIs/mobile apps use bearer tokens (stateless, scalable, no session storage)
Both point to the same validation logic, minimizing duplication.

### 3. **Inclusion of Pre-Built fastapi-users Routers**
Instead of reimplementing login/logout/register, the module reuses fastapi-users' battle-tested implementations. This:
- Reduces security bugs (password hashing, token validation already proven)
- Follows industry standards (OAuth2, OpenID)
- Saves development time
- Makes the custom endpoints (get_me, update_me, set_active_workspace) stand out as PocketPaw-specific logic

### 4. **Workspace as a First-Class Auth Concern**
The `set_active_workspace` endpoint at the auth layer signals that workspace selection is core to the system's identity model, not an afterthought. Users don't just authenticate — they authenticate *into a workspace*.

### 5. **Dependency Injection Over Middleware**
Using `Depends(current_active_user)` rather than middleware for auth checks:
- **Explicit**: each endpoint declares its auth requirement
- **Flexible**: some endpoints could theoretically be public (though none are here)
- **Testable**: dependencies can be easily mocked

## Related Concepts

To fully understand this module, you should also study:
- **FastAPI dependency injection**: How `Depends()` works
- **fastapi-users library**: The OAuth2 framework underpinning auth
- **JWT and bearer tokens**: Stateless authentication for APIs
- **Workspace scoping**: How multi-tenant separation works in PocketPaw
- **AuthService** (`ee.cloud.auth.service`): The business logic layer
- **Authentication Core** (`ee.cloud.auth.core`): Backend and dependency configuration

---

## Related

- [schemas-pydantic-requestresponse-data-models-for-workspace-domain-operations](schemas-pydantic-requestresponse-data-models-for-workspace-domain-operations.md)
- [untitled](untitled.md)
- [license-enterprise-license-validation-and-feature-gating-for-cloud-deployments](license-enterprise-license-validation-and-feature-gating-for-cloud-deployments.md)
- [deps-fastapi-dependency-injection-layer-for-cloud-router-authentication-and-auth](deps-fastapi-dependency-injection-layer-for-cloud-router-authentication-and-auth.md)
- [core-enterprise-jwt-authentication-with-cookie-and-bearer-transport-for-fastapi](core-enterprise-jwt-authentication-with-cookie-and-bearer-transport-for-fastapi.md)
- [group-multi-user-chat-channels-with-ai-agent-participants](group-multi-user-chat-channels-with-ai-agent-participants.md)
- [message-data-model-for-group-chat-messages-with-mentions-reactions-and-threading](message-data-model-for-group-chat-messages-with-mentions-reactions-and-threading.md)
- [backendadapter-adapter-that-makes-pocketpaws-agent-backends-usable-as-knowledge](backendadapter-adapter-that-makes-pocketpaws-agent-backends-usable-as-knowledge.md)
- [workspace-data-model-for-organization-workspaces-in-multi-tenant-enterprise-depl](workspace-data-model-for-organization-workspaces-in-multi-tenant-enterprise-depl.md)
- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
