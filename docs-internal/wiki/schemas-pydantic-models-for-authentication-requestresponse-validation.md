# schemas — Pydantic models for authentication request/response validation

> This module defines three Pydantic BaseModel classes that standardize the shape of authentication-related HTTP requests and responses across the PocketPaw auth domain. It exists as a separate schemas module to centralize data validation contracts, enabling clean separation between HTTP layer concerns (routers) and business logic (services), and ensuring consistency across multiple consumers that import from this file.

**Categories:** auth domain, API schemas and data models, HTTP validation layer, system-wide contracts  
**Concepts:** ProfileUpdateRequest, SetWorkspaceRequest, UserResponse, Pydantic BaseModel, from_attributes (ORM integration), HTTP request/response validation, partial updates (PATCH semantics), multi-workspace architecture, type safety, schema-driven API design  
**Words:** 1457 | **Version:** 1

---

## Purpose

The `schemas` module is the **contract layer** for the authentication domain. It serves as the single source of truth for what request bodies and response bodies should look like when clients interact with auth endpoints.

Why separate it from service or router logic? Because:

1. **Validation Separation**: Pydantic handles all input validation automatically. When a router receives a request, Pydantic validates it against one of these schemas before the route handler even runs.
2. **Reusability**: Multiple parts of the system need to reference the same shape—routers validate against them, services may reference them for type hints, and external clients can inspect them for API documentation.
3. **Contract Clarity**: These schemas act as the documented interface between the HTTP layer and internal services. They define what the system will accept and what it will return.
4. **Evolutionary Flexibility**: If you need to change response structure, you change it here once, and all consumers (routers, services, message handlers, websockets, agent bridge) automatically adapt.

## Key Classes and Methods

### `ProfileUpdateRequest`

**Purpose**: Validates partial user profile updates. Allows clients to update any combination of display name, avatar, and status.

**Fields**:
- `full_name: str | None = None` — User's display name. Optional; `None` means "don't change this."
- `avatar: str | None = None` — Avatar URL or image data. Optional.
- `status: str | None = None` — User status message (e.g., "In a meeting"). Optional.

**Business Logic**: This is a **partial update** schema—all fields are nullable by design. The service layer (likely `AuthService`) receives this, checks which fields are non-`None`, and only updates those attributes. This prevents accidental overwrites of unchanged fields.

**Usage Pattern**: When a client sends `PATCH /users/profile`, the request body is validated against this schema before reaching the handler.

### `SetWorkspaceRequest`

**Purpose**: Validates workspace activation requests. When a user has access to multiple workspaces, they must explicitly select one as their active workspace.

**Fields**:
- `workspace_id: str` — Required identifier of the workspace to activate. Non-optional; the request is invalid without it.

**Business Logic**: This is a **required-field** schema. Setting a workspace is a deliberate action, not optional. The service layer will:
1. Verify the user has access to this workspace (authorization check)
2. Update the user's `active_workspace` field
3. Possibly trigger downstream effects (reload configuration, reset cached permissions, etc.)

**Usage Pattern**: `POST /workspaces/set` or similar endpoint. Used by frontend when user clicks "Switch Workspace."

### `UserResponse`

**Purpose**: Serializes authenticated user data back to clients. This is the response schema for login, profile fetch, or token refresh endpoints.

**Fields**:
- `id: str` — Unique user identifier (likely UUID or MongoDB ObjectId string)
- `email: str` — User's email address
- `name: str` — Display name
- `image: str` — Avatar URL or data URI
- `email_verified: bool` — Whether email has been verified
- `active_workspace: str | None = None` — Currently selected workspace ID, or `None` if not set
- `workspaces: list[dict]` — Array of workspace objects the user can access. Each dict likely contains `{"id": "...", "name": "...", ...}` structure.

**Pydantic Config**: `model_config = {"from_attributes": True}` enables Pydantic to accept ORM objects (e.g., SQLAlchemy models or Beanie documents) and extract attributes automatically. This means a service can do:
```python
user_doc = User.get(user_id)  # Returns ORM/Beanie object
return UserResponse.model_validate(user_doc)  # Pydantic extracts attributes automatically
```

Without this config, you'd need to manually map: `UserResponse(id=user_doc.id, email=user_doc.email, ...)` on every response.

**Business Logic**: This schema defines the "current user" contract. Whenever any handler needs to return user info, it uses this schema. The presence of `workspaces` (plural) indicates the system supports **multi-workspace architecture**—a single user can belong to multiple workspaces and switch between them.

## How It Works

### Request Validation Flow

1. **Client sends HTTP request** with JSON body
2. **FastAPI router decorator** specifies a schema class (e.g., `@router.patch("/profile", model=ProfileUpdateRequest)`)
3. **Pydantic parses and validates** the incoming JSON against the schema
4. **If valid**: Request handler receives a typed Python object (e.g., `profile_update: ProfileUpdateRequest`)
5. **If invalid**: FastAPI returns `422 Unprocessable Entity` with detailed validation errors; handler never runs

### Response Serialization Flow

1. **Service layer returns domain object** (e.g., a Beanie `User` document or ORM model)
2. **Router calls** `UserResponse.model_validate(user_doc)`
3. **Pydantic extracts fields** (using `from_attributes=True`) and builds a `UserResponse` instance
4. **FastAPI serializes** the `UserResponse` to JSON and sends it to client
5. **Client receives** guaranteed-valid shape

### Edge Cases

- **Partial updates**: `ProfileUpdateRequest` allows all-`None` fields. A client could send `{}` (empty JSON object). The service must handle this (likely doing nothing) rather than failing.
- **Workspace access control**: `SetWorkspaceRequest` only contains an ID. The **service layer** must verify the user actually has access to that workspace. This schema doesn't enforce that.
- **Missing workspaces**: If a user has no workspaces, `workspaces: list[dict]` will be an empty list `[]`. Frontend must handle this gracefully.
- **Null active_workspace**: A newly registered user might not have set an active workspace yet, so this field could be `None`.

## Authorization and Security

These schemas do **not** contain authorization logic—they only validate structure and types. Authorization happens at the **service or router middleware level**:

- **ProfileUpdateRequest**: Only the authenticated user (or admins) can update their own profile. Router middleware checks `request.user.id == profile_owner_id`.
- **SetWorkspaceRequest**: Router/service must verify the user is a member of the target workspace. This prevents users from "switching" to workspaces they don't belong to.
- **UserResponse**: Never expose sensitive fields (e.g., password hashes, API keys). This schema only includes safe-to-expose fields.

## Dependencies and Integration

### What imports this module?

From the import graph, **5 files depend on these schemas**:

1. **router** (`ee/cloud/auth/router.py`) — Uses all three schemas as request/response models for HTTP endpoints
2. **service** (`ee/cloud/auth/service.py`) — May use as type hints for return values
3. **group_service** (`ee/cloud/group_service.py` or similar) — Likely returns `UserResponse` when group operations affect users
4. **message_service** (`ee/cloud/message_service.py`) — May return user info in message payloads; uses `UserResponse`
5. **ws** (WebSocket handler) — Sends `UserResponse` in WebSocket messages to connected clients
6. **agent_bridge** (Agent/AI integration) — Returns user info when agent needs context about who initiated a request

This wide distribution indicates that **user response format is a system-wide contract**—it's not just an auth concern, but part of the core data model visible throughout the application.

### What does this module depend on?

Minimal dependencies:
- **pydantic** (standard library import) — Provides `BaseModel`
- **Python 3.10+** (type hints use `X | None` syntax instead of `Union[X, None]`)

No domain dependencies, no circular imports. This is by design—schemas modules should be dependency-light so they can be imported everywhere without creating dependency cycles.

## Design Decisions

### 1. Pydantic BaseModel (not dataclasses or TypedDict)

Why not `@dataclass` or `TypedDict`?

- **Validation**: `BaseModel` validates on instantiation. `@dataclass` does not.
- **Serialization**: `BaseModel.model_dump()` and `model_dump_json()` are built-in. Dataclasses need manual serialization.
- **ORM integration**: `from_attributes=True` bridges ORM objects easily. Dataclasses don't have this.
- **JSON schema generation**: FastAPI auto-generates OpenAPI docs from Pydantic schemas. Dataclasses don't integrate as cleanly.

### 2. Partial vs. Required Fields

- `ProfileUpdateRequest`: All fields optional (`None` defaults) — **partial update pattern**
- `SetWorkspaceRequest`: Required `workspace_id` — **explicit command pattern**
- `UserResponse`: All fields required (no defaults) — **complete data contract**

This design mirrors REST semantics: `PATCH` (partial), `POST` (explicit action), `GET` (full state).

### 3. `active_workspace: str | None`

Why nullable? Because:
- A newly registered user might not have selected a workspace yet
- A user's only workspace might have been deleted or they lost access
- Lazy initialization—don't force workspace selection during signup

Frontend must handle `None` gracefully (prompt user to select workspace, or auto-assign one).

### 4. `workspaces: list[dict]` (not `list[WorkspaceResponse]`)

Why use `list[dict]` instead of a separate `WorkspaceResponse` schema?

Likely reasons:
1. **Simplicity**: Workspace details aren't standardized yet, or vary by context
2. **Flexibility**: Each workspace object might have different fields (metadata, permissions, role, etc.) without needing another schema
3. **Deferred definition**: Workspace schema might live in a separate `workspace/schemas.py` module, and auth module avoids the cross-domain dependency

This is a trade-off: flexibility vs. type safety. As the system matures, this might become `list[WorkspaceResponse]` to add structure.

### 5. `from_attributes = True` Config

This is a **Pydantic v2 convention** (previously `orm_mode = True` in v1). It assumes:
- Domain objects are ORM models or Beanie documents
- They have attributes matching schema field names
- No custom mapping logic needed in services

This keeps services thin: no manual `UserResponse(id=..., email=...)` boilerplate.

## Related Concepts

- **Request/Response Validation**: Core HTTP pattern. Schemas = contracts.
- **ORM Integration**: `from_attributes` bridges database models to HTTP responses.
- **Multi-workspace Architecture**: The presence of `active_workspace` and `workspaces` list indicates the system supports user-to-many-workspaces relationships.
- **Partial Updates**: `ProfileUpdateRequest` with nullable fields is PATCH semantics.
- **Type Safety with Pydantic**: Compile-time type hints + runtime validation.

---

## Related

- [untitled](untitled.md)
