# workspace — Data model for organization workspaces in multi-tenant enterprise deployments

> This module defines the core data models that represent a workspace: the container for an organization's entire deployment in PocketPaw's multi-tenant architecture. It includes Workspace (the main organizational entity with billing/licensing info) and WorkspaceSettings (configurable policies). The module exists as a separate layer to cleanly separate data persistence concerns from business logic, and serves as the contract between the database, service layer, and API routers.

**Categories:** data model, workspace management, multi-tenancy, MongoDB persistence  
**Concepts:** Workspace, WorkspaceSettings, TimestampedDocument, soft delete, deleted_at, multi-tenancy, workspace scoping, slug, Indexed, unique constraint  
**Words:** 1857 | **Version:** 1

---

## Purpose

This module is the **data persistence layer** for workspaces — the organizational unit in PocketPaw's multi-tenant SaaS architecture. In a multi-tenant system, one workspace = one enterprise customer or organization. Every user, agent, conversation, and data artifact belongs to exactly one workspace.

The module exists to:
1. **Define the schema** — What data is required to represent a workspace in the database?
2. **Enforce constraints** — Ensure workspace slugs are globally unique, define default values for settings
3. **Provide type safety** — Give the rest of the codebase a single source of truth for workspace structure (used by `router` and `service` modules)
4. **Enable Beanie integration** — Connect to MongoDB via the Beanie ODM with proper indexing

In the larger architecture, this is a **foundational domain model**. Most other operations in the system are scoped by workspace: you cannot query agents or conversations without specifying which workspace they belong to. This module is the root of that scoping hierarchy.

## Key Classes and Methods

### WorkspaceSettings
**Purpose**: Encapsulates configurable policies and defaults for a workspace. Not all settings need to be set at workspace creation; they can have sensible defaults.

**Fields**:
- `default_agent: str | None` — The ID of the agent that should be used by default in this workspace (e.g., when creating a new conversation without specifying an agent). `None` means the workspace hasn't set a default.
- `allow_invites: bool = True` — Whether users in this workspace can invite others. Controls team expansion permissions. Defaults to `True` (open to invites) to encourage collaboration.
- `retention_days: int | None = None` — Data retention policy: how many days to keep conversation history and logs. `None` means keep forever (unlimited retention). Important for compliance and cost management in enterprise deployments.

**Business Logic**: This is a **settings/configuration object**, not a document. It's embedded within a Workspace record, not stored separately. This means every workspace query returns its settings inline, avoiding extra database lookups for common configuration queries.

### Workspace(TimestampedDocument)
**Purpose**: The core organizational entity. Represents one customer/tenant in the multi-tenant system.

**Fields**:
- `name: str` — Human-readable workspace name (e.g., "Acme Corporation"). Not necessarily unique globally.
- `slug: Indexed(str, unique=True)` — URL-friendly identifier (e.g., "acme-corp"). Must be **globally unique** across all workspaces (enforced by MongoDB unique index). Used in URLs and programmatic references. The `Indexed(unique=True)` tells Beanie to create a database index and constraint.
- `owner: str` — User ID of the admin/owner who created this workspace. This is a foreign key reference to a User document (though not explicitly enforced here). The owner typically has full permissions to delete or reconfigure the workspace.
- `plan: str = "team"` — The subscription tier/license type. Valid values are `"team"`, `"business"`, `"enterprise"`. Determines what features are available and how many seats are granted. Sourced from the licensing system.
- `seats: int = 5` — Number of licensed user seats for this workspace. Default is 5 (suitable for small teams). Enterprise plans may have higher defaults or unlimited seats.
- `settings: WorkspaceSettings` — The embedded configuration object (see above). Defaults to `WorkspaceSettings()`, which gives all defaults (`default_agent=None`, `allow_invites=True`, `retention_days=None`).
- `deleted_at: datetime | None = None` — **Soft delete** marker. If `None`, the workspace is active. If set to a timestamp, the workspace is logically deleted but the record remains in the database (for audit trails, data recovery, compliance). This is a common pattern in SaaS systems to preserve data integrity.

Inherits from `TimestampedDocument`:
- `created_at: datetime` — When the workspace was created (auto-set by base class)
- `updated_at: datetime` — When the workspace was last modified (auto-updated by base class)
- `_id: PydanticObjectId` — MongoDB document ID (auto-generated)

**Business Logic**:
- **Workspace Lifecycle**: A workspace starts with `deleted_at=None`. When deleted, the `deleted_at` field is set but the document remains. Queries for active workspaces should filter `deleted_at=None`.
- **Uniqueness Constraint**: The slug must be unique. This is critical for multi-tenancy: if two workspaces had the same slug, URL routing would be ambiguous.
- **Settings Inheritance**: When a new workspace is created, it gets default settings. Users can later update `settings` to customize behavior.
- **Owner as Admin**: The `owner` field identifies who has initial control. Authorization logic (in the `service` or router layer) likely checks if the current user is the owner before allowing destructive operations.
- **Plan-Driven Limits**: The `plan` field gates features. The `seats` field is typically enforced by the service layer: if you try to invite a 6th user to a team plan with 5 seats, the service rejects it.

**Beanie Integration**:
- Inherits from `TimestampedDocument` (defined in `ee.cloud.models.base`), which provides MongoDB document lifecycle (timestamps, ID generation).
- The `class Settings` inner class with `name = "workspaces"` tells Beanie to store Workspace documents in the MongoDB collection named `workspaces`.

## How It Works

**Creation Flow**:
1. An API endpoint (in the `router` module) receives a request to create a workspace (e.g., POST `/workspaces` with name, plan, etc.).
2. The router validates the input and calls the `service` layer.
3. The service layer (e.g., `WorkspaceService`) instantiates a Workspace model, sets defaults (like `deleted_at=None`, `settings=WorkspaceSettings()`).
4. Beanie saves it to MongoDB. The base class auto-sets `created_at` and `updated_at`. MongoDB auto-generates `_id`.
5. Beanie enforces the slug uniqueness constraint: if duplicate, it raises an error (caught and returned as HTTP 409 Conflict by the router).

**Retrieval Flow**:
1. Service queries: "Get workspace with slug='acme-corp'" → Beanie builds a MongoDB query and returns a Workspace instance.
2. The caller gets a fully-typed Python object with all fields populated.
3. The settings are already embedded, so no follow-up queries needed.

**Update Flow**:
1. Service retrieves the workspace, modifies a field (e.g., `workspace.plan = "enterprise"` or `workspace.settings.allow_invites = False`).
2. Calls `workspace.save()` (Beanie method). `updated_at` is auto-updated.
3. MongoDB updates just the fields that changed.

**Soft Delete Flow**:
1. Instead of deleting the document, the service sets `workspace.deleted_at = datetime.now()` and calls `save()`.
2. Queries for active workspaces add a filter: `Workspace.find({"deleted_at": None})`.
3. The document remains in the database for compliance/recovery, but is invisible to normal queries.

**Edge Cases**:
- **Duplicate Slug**: If creation tries to use an existing slug, Beanie raises a duplicate key error. The service/router should catch and return a user-friendly error.
- **Settings with None**: Fields like `retention_days=None` and `default_agent=None` are valid. The service layer interprets `None` as "no policy set" or "use system default".
- **Plan Mismatch**: If someone manually sets `plan="invalid"` (not one of the three valid values), Pydantic validation doesn't prevent it (no enum). The service layer should validate plan values.
- **Owner Deletion**: If the user referenced in `owner` is deleted, this model doesn't cascade-delete the workspace (it's just a string ID). The service layer must handle this scenario.

## Authorization and Security

This module **does not enforce authorization directly**. It defines the data structure; authorization is enforced at higher layers:

- **Who can view a workspace?** — Anyone with access to that workspace (determined by the `router` or service via user-workspace membership checks).
- **Who can modify workspace settings?** — Typically the owner (checked by the service before allowing updates).
- **Who can delete a workspace?** — Typically the owner; deletion is a soft delete (set `deleted_at`).
- **Cross-workspace visibility**: The model itself doesn't restrict cross-workspace queries, but the service layer should always filter by workspace when querying user data (e.g., "get agents in workspace X", not "get all agents").

The `slug: Indexed(str, unique=True)` is a technical constraint (uniqueness), not an authorization control.

## Dependencies and Integration

**Depends On**:
- **`ee.cloud.models.base`** — Imports `TimestampedDocument`, the base class that adds MongoDB integration, `_id`, `created_at`, and `updated_at` fields.
- **`beanie`** — The `Indexed` function creates database indexes and constraints. The model inherits Beanie's document methods (`save()`, `find()`, etc.).
- **`pydantic`** — `BaseModel` and `Field` provide data validation, serialization, and field customization. `WorkspaceSettings` is a plain Pydantic model (not a MongoDB document).
- **`datetime`** — Standard library for timestamp types (`created_at`, `updated_at`, `deleted_at`).

**Imported By**:
- **`__init__`** — Re-exports Workspace and WorkspaceSettings so other modules can import from the models package cleanly (`from ee.cloud.models import Workspace`).
- **`router`** — The API layer uses Workspace to define request/response schemas and query parameters. The router calls service methods that return Workspace instances.
- **`service`** — The business logic layer (likely `WorkspaceService`) performs CRUD operations on Workspace instances. It queries the database, validates business rules, and coordinates with other services.

**System Position**:
```
API Router (router.py)
    ↓ calls
WorkspaceService (service.py)
    ↓ uses
Workspace Model (this module) + WorkspaceSettings
    ↓ stored in
MongoDB via Beanie
```

Every other domain model (agents, conversations, users) likely includes a `workspace_id` field to establish which workspace owns the data. This module is the root.

## Design Decisions

**1. Embedded Settings vs. Separate Collection**
- **Choice**: `settings: WorkspaceSettings` is embedded (a nested object), not a separate MongoDB document.
- **Why**: Settings are small, always accessed together with the workspace, and rarely updated independently. Embedding avoids a join and keeps the data model simple.
- **Trade-off**: Can't have separate permission checks on settings (e.g., "readonly user can read workspace but not settings"). Acceptable for most enterprise SaaS.

**2. Soft Deletes with `deleted_at`**
- **Choice**: Deletion sets `deleted_at` instead of removing the document.
- **Why**: Preserves audit trails, enables data recovery, satisfies compliance requirements (GDPR right to erasure can be implemented as data anonymization + soft delete).
- **Cost**: Queries must filter `deleted_at=None`. Requires discipline in the service layer.

**3. Slug as Unique Identifier**
- **Choice**: `slug: Indexed(str, unique=True)` is a unique, human-readable identifier, not just the MongoDB `_id`.
- **Why**: URLs and programmatic references are cleaner with "acme-corp" than with a 24-character hex ObjectId. Enables vanity URLs.
- **Cost**: Slugs are harder to generate safely (must avoid collisions, handle Unicode, etc.). Typically generated from the workspace name and checked for uniqueness.

**4. Plan as String, Not Enum**
- **Choice**: `plan: str = "team"` is a string, not a Python enum.
- **Why**: Flexibility — new plans can be added in the license system without updating this model. Pydantic doesn't restrict to specific values.
- **Cost**: No compile-time safety. The service layer must validate that plan is one of the known values.
- **Better approach**: Would be `plan: Literal["team", "business", "enterprise"]` for type safety, but that's not shown here.

**5. Owner as User ID String, Not Reference**
- **Choice**: `owner: str` is a string (User ID), not a foreign key or reference field.
- **Why**: MongoDB doesn't enforce foreign keys. Document references are intentionally loose (schema flexibility). The service layer assumes the User exists elsewhere.
- **Cost**: Orphaned workspaces if the owner user is deleted. The service must handle this.

**6. Inheritance from TimestampedDocument**
- **Choice**: Workspace extends `TimestampedDocument` (from base.py), gaining `created_at`, `updated_at`, `_id`.
- **Why**: Code reuse. Every document in the system needs timestamps; centralizing in a base class avoids duplication.
- **Pattern**: Common in MongoDB/document-DB-backed services using Beanie or similar ODMs.

**7. Default Values**
- **Choice**: `plan="team"`, `seats=5`, `settings=WorkspaceSettings()`, `deleted_at=None`, `allow_invites=True`.
- **Why**: Sensible defaults reduce the chance of required-field errors. A small workspace can be created with just a name and owner.
- **Business Logic**: "New workspaces are team plans with 5 seats, invites enabled, and no retention limit by default."

---

## Related

- [base-foundational-document-model-with-automatic-timestamp-management-for-mongodb](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md)
- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
- [untitled](untitled.md)
