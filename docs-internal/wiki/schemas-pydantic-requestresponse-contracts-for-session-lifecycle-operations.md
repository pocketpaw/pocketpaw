# schemas — Pydantic request/response contracts for session lifecycle operations

> This module defines the HTTP API contracts (request bodies and response payloads) for the sessions domain using Pydantic BaseModel. It exists to enforce type safety and validation at the API boundary, ensuring that clients can only submit well-formed session creation/update requests and receive consistently-shaped session responses. As a schema module, it serves as the contract layer between the FastAPI router and the business logic, used by 5 downstream consumers (router, service, group_service, message_service, ws, agent_bridge).

**Categories:** sessions domain, API contract layer, data validation, CRUD operations  
**Concepts:** CreateSessionRequest, UpdateSessionRequest, SessionResponse, Pydantic BaseModel, API contract layer, request/response schemas, type validation, soft delete pattern, denormalization, dual ID strategy  
**Words:** 1531 | **Version:** 1

---

## Purpose

This module defines the **data contracts** for HTTP requests and responses in the sessions domain. It solves the problem of:

1. **Type Safety at the Boundary**: FastAPI uses these Pydantic models to validate incoming JSON and automatically reject malformed requests before they reach business logic
2. **Documentation**: The field definitions serve as OpenAPI/Swagger documentation; clients know exactly what fields are required/optional
3. **Consistency**: All callers (HTTP handlers, async services, WebSocket handlers, agent bridges) operate against the same schema definitions, reducing duplication and drift
4. **Decoupling**: Router handlers don't directly depend on the persistence model (MongoDB document); they depend on these schemas, allowing the internal model to evolve without breaking the API

In the system architecture, schemas sit at the **API contract layer**—above the service layer but below HTTP delivery. They transform between the wire format (JSON) and Python objects that services consume.

## Key Classes and Methods

### CreateSessionRequest
Represents the payload required to create a new session.

**Fields:**
- `title: str` — User-facing name for the session. Defaults to `"New Chat"` if omitted, allowing clients to create a session without specifying a title.
- `pocket_id: str | None` — Optional link to a "pocket" (likely a container/project/space concept) at creation time. Clients can omit this to create an unlinked session.
- `group_id: str | None` — Optional association with a group. The system allows null; business logic determines if this is meaningful.
- `agent_id: str | None` — Optional association with an AI agent. Enables agent-specific sessions (e.g., a session for a particular chatbot).

**Business Logic Notes:**
The presence of `pocket_id`, `group_id`, and `agent_id` suggests sessions can exist in multiple organizational contexts. A single session might belong to a workspace, but optionally nest within a pocket, belong to a group, and/or be associated with an agent. The schema doesn't enforce mutual exclusivity, allowing flexible linking strategies.

### UpdateSessionRequest
Represents the payload for partial session updates.

**Fields:**
- `title: str | None` — Update the session title. Null means "don't change."
- `pocket_id: str | None` — Relink or unlink the session from a pocket. Null is semantically ambiguous (does it mean "remove link" or "don't change"?); likely requires careful service-layer interpretation.

**Business Logic Notes:**
Notably, this schema does **not** allow updating `group_id` or `agent_id` after creation. This suggests those associations are considered immutable or require different endpoints. The `pocket_id` is updatable, implying session–pocket relationships are meant to be flexible.

### SessionResponse
The shape of a session in GET responses and after mutations.

**Fields:**
- `id: str` — Primary key (likely MongoDB ObjectId as string)
- `session_id: str` — Unique session identifier. Distinct from `id`; likely a friendly snowflake ID or UUID, used for external APIs and user-facing URLs.
- `workspace: str` — Every session is scoped to a workspace (multi-tenancy isolation)
- `owner: str` — User ID who created/owns the session
- `title: str` — The session's name
- `pocket: str | None` — Denormalized reference to the linked pocket (or null)
- `group: str | None` — Denormalized reference to the linked group (or null)
- `agent: str | None` — Denormalized reference to the linked agent (or null)
- `message_count: int` — Cached count of messages in this session (denormalized for performance)
- `last_activity: datetime` — Timestamp of the most recent message or event
- `created_at: datetime` — Creation timestamp
- `deleted_at: datetime | None` — Soft-delete timestamp. Null means active; non-null means logically deleted but retained for auditing

**Design Notes:**
The response includes both `id` and `session_id`, suggesting internal IDs differ from external IDs. Denormalized fields (`message_count`, `pocket`, `group`, `agent`) indicate the response is pre-computed or aggregated by the service layer, not a direct database dump. The `deleted_at` field reveals a soft-delete strategy (logical deletion with retention).

## How It Works

### Data Flow

1. **Client sends HTTP request** (e.g., POST `/sessions` with JSON body)
2. **FastAPI receives JSON** → **Pydantic validates** against `CreateSessionRequest`
   - If validation fails (missing required field, wrong type), FastAPI returns 422 Unprocessable Entity with detailed errors
   - If valid, FastAPI hydrates the `CreateSessionRequest` object
3. **Router handler receives the validated object** → calls `SessionService.create(request)`
4. **Service layer** transforms the schema into a database model, persists it, and returns a populated `SessionResponse`
5. **Router serializes the response** as JSON and returns it to the client

### Request Validation Examples

**Valid CreateSessionRequest:**
```json
{"title": "Project Planning", "pocket_id": "poc_123", "agent_id": "agent_456"}
```
Will be accepted; `group_id` is inferred as `null`.

**Invalid CreateSessionRequest:**
```json
{"pocket_id": "poc_123"}
```
Will be accepted; `title` defaults to `"New Chat"`, and `group_id`, `agent_id` default to `null`.

**Invalid UpdateSessionRequest:**
```json
{"title": 123}
```
Will be rejected by Pydantic (title must be `str` or `None`, not `int`).

### Edge Cases

1. **Null pocket_id in UpdateSessionRequest**: The schema allows it, but the service layer must decide: does it mean "unlink the pocket" or "don't update the pocket field"? This is a common ambiguity in PATCH operations; the service likely has a convention (e.g., explicit `null` = unlink, field omitted = no change).
2. **Soft Deletes**: The response includes `deleted_at`. Clients should either filter these out or a service layer pre-filters GET responses to exclude soft-deleted sessions.
3. **Denormalization**: Fields like `message_count` and `last_activity` are snapshots at the time of the response. Concurrent messages may age these values immediately; this is a trade-off for read performance.

## Authorization and Security

**Not explicitly defined in this module.** However:

- **Workspace Scoping**: Every session has a `workspace` field. The router/service layer should validate that the authenticated user has access to that workspace before allowing read/write.
- **Ownership**: The `owner` field suggests only the owner (or admins) can update a session.
- **Field Exposure**: The response includes `owner` and `workspace`, allowing clients to verify access control rules client-side or for auditing.

Actual authorization logic lives in the router or a middleware layer (not shown here), but this schema enables those guards by exposing the necessary context.

## Dependencies and Integration

### Consumers (Import Graph)
This module is imported by:

1. **router** — HTTP handlers that accept `CreateSessionRequest` and `UpdateSessionRequest` as body parameters, return `SessionResponse`
2. **service** — The SessionService accepts requests and returns responses; may transform request fields into database operations
3. **group_service** — Likely retrieves sessions linked to a group; uses schemas for type hints and response consistency
4. **message_service** — Operates on sessions; may update `last_activity` or `message_count` fields in the response
5. **ws** — WebSocket handlers that deserialize session data and send `SessionResponse` over the wire
6. **agent_bridge** — External agent integration that reads/writes sessions; needs consistent contracts

### No Internal Dependencies
This module does not import from other modules in the scanned set, keeping it isolated and free from circular dependencies. It only depends on:
- **pydantic** (external): The BaseModel, Field utilities for validation and serialization
- **datetime** (stdlib): For `datetime` type hints

### Integration Pattern
The schema acts as a **contract layer**:
```
HTTP Client
    ↓ (JSON)
  FastAPI Router
    ↓ (CreateSessionRequest object)
  SessionService
    ↓ (transforms to DB model, executes logic)
  MongoDB
    ↓ (fetches/persists)
  SessionService
    ↓ (transforms DB model to SessionResponse)
  FastAPI Router
    ↓ (JSON serialization via Pydantic)
HTTP Client
```

Each layer depends on the schema contracts, but not on each other's internal representations.

## Design Decisions

### 1. **Dual ID Strategy** (`id` vs. `session_id`)
- `id`: Likely the MongoDB ObjectId, kept internal for direct database queries
- `session_id`: A friendly, external ID (possibly shorter, more readable)
- **Rationale**: Decouples the public API from database internals; allows ID rotation or migration without breaking clients

### 2. **Soft Deletes via `deleted_at` Field**
- Sessions are never fully deleted; only marked with a `deleted_at` timestamp
- **Rationale**: Preserves audit trails, allows recovery, and enables "trash" features. Services must explicitly filter by `deleted_at IS NULL` in queries.

### 3. **Denormalized Fields in Response** (`message_count`, `pocket`, `group`, `agent`, `last_activity`)
- These are not raw database fields but computed/cached values
- **Rationale**: Improves client UX (no need for extra round-trips to fetch metadata) and read performance (precomputed aggregations)
- **Trade-off**: Write-path complexity; services must update these fields when related data changes

### 4. **Optional Associations** (`pocket_id`, `group_id`, `agent_id` all nullable)
- Sessions can exist without any of these links
- **Rationale**: Flexibility; different use cases may require different organizational structures (standalone sessions, pocket-scoped, group-scoped, or agent-specific)

### 5. **Immutable Group and Agent Associations**
- `UpdateSessionRequest` does not allow changing `group_id` or `agent_id`
- **Rationale**: Likely these are architectural dependencies that should not be reassigned post-creation; changing them might violate business logic or require cascade operations
- **Pocket is mutable**: Suggests pockets are more like tags or lightweight containers; sessions can move between them

### 6. **Pydantic's `from_attributes=True` (implicit)**
While not shown, FastAPI likely configures Pydantic with `from_attributes=True` to allow automatic ORM object serialization (MongoDB documents to SessionResponse). The service layer likely uses this to cast database objects directly to the schema.

## Architectural Context

**Schemas** are part of the **API layer**, sitting between:
- **Presentation** (HTTP, WebSocket, external APIs) — receives/returns these models
- **Business Logic** (Service layer) — consumes and produces these models
- **Persistence** (MongoDB models) — different structure, transformed to/from schemas

This module enforces the **contract-first** design pattern: the API contract is explicit and comes before implementation, reducing surprises and enabling early validation.

---

## Related

- [untitled](untitled.md)
