# schemas — Pydantic request/response data models for workspace domain operations

> This module defines the contract between the workspace API layer and its consumers by providing Pydantic data models for validating incoming requests and serializing outgoing responses. It exists to centralize workspace-related data validation and type safety in one place, ensuring consistency across the router, service layer, and external integrations (agent_bridge, ws) that need to understand workspace operations. It serves as the domain-level API boundary for all workspace CRUD, invite management, and member role operations.

**Categories:** workspace domain, API contract layer, data validation, schema definition, Pydantic DTOs  
**Concepts:** CreateWorkspaceRequest, UpdateWorkspaceRequest, CreateInviteRequest, UpdateMemberRoleRequest, WorkspaceResponse, MemberResponse, InviteResponse, validate_slug, field_validator, BaseModel  
**Words:** 1866 | **Version:** 1

---

## Purpose

The `schemas` module is a **data contract definition layer** that sits between the HTTP API and the business logic. Its primary purposes are:

1. **Input Validation**: Validates incoming HTTP requests before they reach service logic, catching malformed data early (e.g., slug format, role values)
2. **Type Safety**: Provides structured typing through Pydantic BaseModel, enabling IDE autocomplete, static analysis, and runtime validation
3. **API Documentation**: Serves as the source of truth for what the workspace API accepts and returns, automatically documenting endpoints
4. **Cross-Layer Contract**: Creates a shared language between the router (HTTP layer), service layer, and external systems (agent_bridge for AI operations, ws for real-time events)

This is a **stateless, declarative module** — it contains no business logic, only schema definitions. It's imported by multiple downstream consumers (router, service, group_service, message_service, ws, agent_bridge) because they all need to understand the same data structures.

## Key Classes and Methods

### Request Classes (Input Validation)

#### `CreateWorkspaceRequest`
**Purpose**: Validates the creation of a new workspace.

**Fields**:
- `name` (str, 1-100 chars): The human-readable workspace name
- `slug` (str, 1-50 chars): The URL-safe identifier for the workspace (e.g., "my-team-workspace")

**Validation Logic**:
- `validate_slug()` method enforces that slugs match the pattern `^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$`
  - Must start and end with alphanumeric characters
  - Can contain hyphens in the middle
  - Must be lowercase only
  - This prevents invalid URLs and domain-like identifiers

**Business Reason**: Slugs are used in URLs (`/workspace/{slug}`), so they must be URL-safe and readable. Restricting to lowercase and hyphens ensures consistency across the system.

#### `UpdateWorkspaceRequest`
**Purpose**: Validates partial updates to an existing workspace.

**Fields**:
- `name` (str | None): Optional new workspace name
- `settings` (dict | None): Optional workspace-level configuration (flexible schema for future extensibility)

**Business Reason**: All fields are optional (`None`), allowing clients to update only what they need. This is standard REST PATCH semantics.

#### `CreateInviteRequest`
**Purpose**: Validates the creation of a workspace member invitation.

**Fields**:
- `email` (str): The email address of the person being invited
- `role` (str, default="member"): The role granted to the invitee, restricted to `"admin"` or `"member"`
- `group_id` (str | None): Optional group assignment upon joining (if workspace uses group-based organization)

**Business Reason**: The role field uses a strict enum pattern (`^(admin|member)$`) to prevent invalid role assignments. The inviter shouldn't be able to create invites with invalid roles. Note that "owner" is NOT allowed here — ownership is likely assigned through different logic.

#### `UpdateMemberRoleRequest`
**Purpose**: Validates role changes for existing workspace members.

**Fields**:
- `role` (str): The new role, restricted to `"owner"`, `"admin"`, or `"member"`

**Business Reason**: Unlike `CreateInviteRequest`, this allows promotion to "owner". The pattern `^(owner|admin|member)$` ensures only valid roles are accepted. This prevents typos or injection attacks that might otherwise bypass authorization checks.

### Response Classes (Output Serialization)

#### `WorkspaceResponse`
**Purpose**: The canonical representation of a workspace returned by the API.

**Fields**:
- `id`, `name`, `slug`: Core workspace identity
- `owner` (str): The ID or email of the workspace owner
- `plan` (str): The billing plan tier (e.g., "free", "pro", "enterprise") — used by downstream services to determine feature availability
- `seats` (int): The number of member seats available on the plan
- `created_at` (datetime): Workspace creation timestamp
- `member_count` (int): Current number of active members (default 0 if not populated)

**Usage**: Returned by workspace creation, fetch, and list endpoints. The router and service layer populate this with data from the database, and it's sent to clients and potentially to agent_bridge for AI agents to understand workspace capacity and configuration.

#### `MemberResponse`
**Purpose**: Represents a workspace member in API responses.

**Fields**:
- `id`, `email`, `name`, `avatar`: Member identity and profile
- `role` (str): The member's current role (owner/admin/member)
- `joined_at` (datetime): When the member joined the workspace

**Usage**: Returned when listing workspace members or fetching member details. The avatar field allows the UI to display member pictures. The `joined_at` field provides audit information.

#### `InviteResponse`
**Purpose**: Represents a pending or accepted workspace invitation.

**Fields**:
- `id`, `email`, `role`: Invitation core data
- `invited_by` (str): The ID/email of who sent the invitation (for audit trail)
- `token` (str): The unique acceptance token (used in accept-invite endpoints, typically sent via email)
- `accepted`, `revoked`, `expired` (bool): Invitation status flags
- `expires_at` (datetime): When the invitation becomes invalid

**Business Reason**: Separating invitation state into three boolean fields (`accepted`, `revoked`, `expired`) makes the state machine explicit. An invitation can be revoked before expiration, or naturally expire. The token is a security credential that prevents anyone with just the email from accepting an invite.

## How It Works

### Data Flow

1. **Inbound Request**: An HTTP client sends a POST to `/workspace/create` with a JSON body
2. **Pydantic Validation**: FastAPI (used by the router) automatically instantiates `CreateWorkspaceRequest` from the JSON. If validation fails (e.g., slug has uppercase letters), Pydantic raises a validation error and FastAPI returns a 422 Unprocessable Entity response
3. **Service Layer Call**: If validation passes, the router calls the service layer with the validated request object
4. **Database Operation**: The service layer creates the workspace in the database
5. **Response Serialization**: The service returns data that's mapped into `WorkspaceResponse`, which FastAPI serializes to JSON
6. **Client Receipt**: The client receives the workspace details

### Cross-System Usage

- **router**: Uses request classes to validate incoming HTTP bodies, response classes to serialize database objects
- **service**: Accepts request objects, uses them to validate/transform data before database operations, returns raw data that service consumers (router) serialize using response classes
- **group_service**, **message_service**: May depend on response schemas when operating within workspace scope (e.g., verifying workspace exists before creating groups/messages)
- **ws** (WebSocket handler): Uses response classes to serialize real-time workspace events sent to connected clients
- **agent_bridge**: Uses response classes to understand workspace structure and permissions when executing AI agent operations (e.g., an AI agent needs to know the `plan` to determine available features)

### Edge Cases and Validation

- **Slug Validation**: The regex `^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$` allows single-character slugs (second alternative) or multi-character slugs with hyphens in the middle. This prevents invalid slugs like `-invalid`, `invalid-`, or `INVALID`.
- **Optional Fields**: `UpdateWorkspaceRequest` and `CreateInviteRequest.group_id` are optional, allowing partial updates and conditional group assignment.
- **Role Enums**: The strict pattern on role fields prevents invalid values. If a future role type is added (e.g., "editor"), all these patterns must be updated simultaneously — this is intentional to force explicit migration.

## Authorization and Security

This module defines the **shape** of data but not the **authorization logic**. However, the schemas support authorization checks downstream:

- **Role Pattern Restrictions**: By restricting roles to known values (`admin|member|owner`), the schemas prevent role injection attacks. A malicious client cannot craft a request with `role="superuser"` — Pydantic will reject it.
- **Slug Format**: The slug validation prevents directory traversal or injection attacks that might exploit URL patterns (e.g., `/workspace/../../admin`).
- **Token in InviteResponse**: The `token` field is a security credential. Only the legitimate invitee who receives the email should have this token. The service layer (not this module) is responsible for validating the token matches the email before accepting an invite.
- **No Password Fields**: Notably, these schemas don't include passwords. Password management is likely handled in a separate auth module, which is good security practice (separation of concerns).

**Authorization is enforced upstream**: The router layer uses these schemas to validate format, then calls authorization middleware/decorators to check whether the requesting user is allowed to perform the operation (e.g., only workspace owners can update workspace settings).

## Dependencies and Integration

### Internal Dependencies
- **pydantic** (BaseModel, Field, field_validator): Core data validation framework. No database ORM (Beanie, SQLAlchemy) appears in this module, keeping it framework-agnostic.
- **datetime**: Used in `WorkspaceResponse`, `MemberResponse`, `InviteResponse` for timestamps.
- **re**: Used for slug pattern validation.

### Consumers (Inbound Dependencies)
- **router** (`/cloud/workspace/router.py`): Uses request schemas to validate API payloads, response schemas to serialize responses.
- **service** (`/cloud/workspace/service.py`): Accepts request objects, returns data that response classes wrap.
- **group_service**, **message_service**: May validate operations within workspace scope using response schemas.
- **ws** (WebSocket): Serializes real-time workspace events using response classes.
- **agent_bridge**: Deserializes `WorkspaceResponse` to understand workspace configuration for AI operations.

### Design Pattern: Request/Response Separation
This module uses the **DTO (Data Transfer Object) pattern**, split into two categories:
- **Request DTOs**: Validate and shape client input
- **Response DTOs**: Serialize and shape service output

This separation allows the service layer to accept flexible input and return rich output without coupling the HTTP contract to the database model.

## Design Decisions

### 1. **Pydantic BaseModel over Dataclasses**
Pydantic was chosen (not standard dataclasses) because it provides runtime validation, serialization, and automatic OpenAPI documentation generation. Dataclasses would require manual validation logic.

### 2. **Regex Validation for Slug**
The `validate_slug()` method uses a custom regex pattern rather than a library-provided slug validator. This suggests:
- **Explicit Control**: The team wanted precise control over what constitutes a valid slug in their domain (e.g., hyphens allowed, single-char allowed).
- **Documentation**: The pattern is readable and self-documenting.
- **No External Dependencies**: Avoids a library import for a simple pattern.

### 3. **Optional Fields in Update Requests**
`UpdateWorkspaceRequest` uses `| None` syntax (Python 3.10+ union types) for all fields. This allows clients to omit fields they don't want to change, implementing proper REST PATCH semantics.

### 4. **Separate CreateInviteRequest and UpdateMemberRoleRequest**
These could have been a single schema, but they're separate because:
- **Different Constraints**: CreateInviteRequest restricts roles to `admin|member` (logical: you can't invite someone as an owner). UpdateMemberRoleRequest allows `owner|admin|member` (logical: you can promote a member to owner).
- **Different Fields**: CreateInviteRequest has `group_id`; UpdateMemberRoleRequest doesn't.
- **Intent Clarity**: Separate classes make the intent explicit in the code and API documentation.

### 5. **Flexible Settings Field**
`UpdateWorkspaceRequest.settings` is typed as `dict | None`, not a strict schema. This suggests:
- **Forward Compatibility**: Settings can evolve without schema changes.
- **Trade-off**: Loses validation of settings structure at the schema layer. Validation is pushed to the service layer or database layer.

### 6. **Field Defaults and Patterns**
- `CreateInviteRequest.role` defaults to `"member"` — most invitations are probably member-level, so the client doesn't need to specify it.
- Role fields use `pattern` rather than an enum. Pydantic enums would be stricter but less flexible if roles change. Patterns are validated at serialization but allow the underlying data to be a string.

### 7. **Explicit Boolean Flags in InviteResponse**
Instead of a single `status` enum field (e.g., `status: "pending" | "accepted" | "expired"`), the schema uses three booleans: `accepted`, `revoked`, `expired`. This allows the database/service to represent states more flexibly (e.g., an expired invite can also be marked as revoked). The downside is that clients need to interpret multiple flags, but this is likely intentional to support complex state machines.

## Architectural Notes

- **Stateless and Declarative**: This module has no state, no async operations, no side effects. It's purely a declarative contract.
- **Framework Agnostic (Almost)**: The only framework dependency is Pydantic. The schemas don't import from FastAPI, database, or service modules, making them portable.
- **Single Responsibility**: Each class is focused on a single operation (Create, Update, Response), following the Single Responsibility Principle.
- **Validation as a Defensive Layer**: By validating at the schema layer, the downstream service and database layers can assume data is well-formed, reducing defensive programming and bugs.

---

## Related

- [untitled](untitled.md)
