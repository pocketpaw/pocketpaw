# pocket — Data models for Pocket workspaces with widgets, teams, and collaborative agents

> This module defines the core document models (Pocket, Widget, WidgetPosition) that represent collaborative workspaces in the OCEAN platform. Pockets are the primary workspace container that hold widgets (UI components), team members, and assigned agents, with support for sharing and ripple specifications. It exists as a separate module to establish the authoritative schema and enable other services (pocket service, event handlers, API layer) to work with a consistent, validated data structure.

**Categories:** workspace management, data model / schema, collaborative features, CRUD, document structure  
**Concepts:** Pocket (workspace container), Widget (embedded UI component), WidgetPosition (grid layout), TimestampedDocument (base class with created_at/updated_at), Beanie ODM (MongoDB object mapping), Pydantic model validation, Field aliases (camelCase ↔ snake_case), Workspace scoping (multi-tenancy), Visibility enum (private/workspace/public), Share link token (anonymous access)  
**Words:** 1788 | **Version:** 1

---

## Purpose

The `pocket` module defines the data layer for Pocket workspaces — the core collaborative workspace abstraction in OCEAN. A Pocket is a container that:
- Organizes widgets (customizable UI components) on a visual grid
- Associates a team of users and intelligent agents
- Enables sharing with fine-grained access control (private/workspace/public)
- Optionally defines a "ripple spec" — a workflow or automation specification

This module exists because:
1. **Schema Definition**: It's the single source of truth for how workspace data is structured, validated, and persisted to MongoDB via Beanie ODM
2. **Frontend-Backend Alignment**: Field aliases (e.g., `dataSourceType` → `_dataSourceType` for JSON) ensure the Python backend and JavaScript frontend speak the same language
3. **Type Safety**: Pydantic models provide runtime validation and IDE support for code using these objects
4. **Cross-Functional Integration**: By centralizing the schema, services (pocket service), event handlers, and API routers can all depend on this single definition

## Key Classes and Methods

### WidgetPosition

**Purpose**: A lightweight coordinate model for placing widgets on a grid-based layout.

**Fields**:
- `row: int = 0` — Grid row index
- `col: int = 0` — Grid column index

**Design Note**: This is a simple, reusable subdocument. It doesn't need MongoDB persistence concerns because it's always embedded within a Widget.

---

### Widget

**Purpose**: A Pydantic subdocument representing a single UI widget embedded within a Pocket. Widgets are the building blocks of the workspace — each one can display data, execute actions, or represent an agent interface.

**Key Design Decision**: Widgets have their own `id` field (aliased as `_id` in JSON) so the frontend can address and update widgets by ID rather than by array index. This makes widget references resilient to reordering.

**Fields**:

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `id` | str | UUID from ObjectId | Aliased as `_id` for frontend; allows direct widget addressing |
| `name` | str | Required | Display name for the widget |
| `type` | str | "custom" | Widget category; could be "chart", "table", "agent-panel", etc. |
| `icon` | str | "" | Icon identifier (CSS class, emoji, or URL) |
| `color` | str | "" | Color for UI theming |
| `span` | str | "col-span-1" | Tailwind CSS grid span class (e.g., "col-span-2" for wider widgets) |
| `dataSourceType` | str | "static" | How data is populated: "static" (hardcoded), "dynamic" (fetched), "agent" (from an agent), etc. |
| `config` | dict | {} | Type-specific configuration; structure depends on `type` |
| `props` | dict | {} | Runtime properties passed to the widget renderer |
| `data` | Any | None | The actual data displayed by the widget (cached or computed) |
| `assignedAgent` | str \| None | None | ID of an agent assigned to this widget (if applicable) |
| `position` | WidgetPosition | Default(0,0) | Grid placement |

**Pydantic Configuration**:
- `populate_by_name = True`: Accepts both snake_case Python names and camelCase aliases (e.g., both `dataSourceType` and `data_source_type`)
- This is essential for bidirectional API compatibility

---

### Pocket

**Purpose**: The primary workspace document. Inherits from `TimestampedDocument` (providing `created_at` and `updated_at` timestamps) and represents a collaborative workspace with widgets, team management, and sharing controls.

**Key Design Decisions**:
1. **Workspace Scoping**: Indexed on `workspace` field for efficient tenant isolation
2. **Flexible Team/Agent References**: `team` and `agents` fields are typed as `list[Any]` to support both ID strings and populated objects (relationship flexibility)
3. **Visibility + Sharing**: Combines a visibility enum (private/workspace/public) with explicit `shared_with` list for granular access control
4. **Ripple Spec**: Optional field for complex workflow automation specs (decoupled from the Pocket schema)

**Fields**:

| Field | Type | Default | Constraints | Purpose |
|-------|------|---------|-------------|----------|
| `workspace` | Indexed(str) | Required | Indexed for queries | Tenant/workspace ID for multi-tenancy |
| `name` | str | Required | — | Human-readable workspace name |
| `description` | str | "" | — | Optional long-form description |
| `type` | str | "custom" | No enum — flexible | Category: "deep-work", "data", "custom", etc. |
| `icon` | str | "" | — | UI representation |
| `color` | str | "" | — | UI theming |
| `owner` | str | Required | — | User ID of workspace creator |
| `team` | list[Any] | [] | — | User IDs or populated User objects (lazy or eager loading) |
| `agents` | list[Any] | [] | — | Agent IDs or populated Agent objects |
| `widgets` | list[Widget] | [] | — | Embedded Widget subdocuments |
| `rippleSpec` | dict \| None | None | — | Optional workflow/automation config; structure TBD by feature |
| `visibility` | str | "private" | `^(private\|workspace\|public)$` | Scope of default access |
| `share_link_token` | str \| None | None | — | Anonymous share token (if public via link) |
| `share_link_access` | str | "view" | `^(view\|comment\|edit)$` | Permission level for shared link |
| `shared_with` | list[str] | [] | — | Explicit user IDs with granted access (overrides visibility) |

**Pydantic Configuration**:
- `populate_by_name = True`: Supports both snake_case and camelCase

**MongoDB Settings**:
- `name = "pockets"`: Collection name in MongoDB
- Inherits timestamp management from `TimestampedDocument`

---

## How It Works

### Data Flow

1. **Creation**: Frontend sends a JSON payload with camelCase fields (e.g., `{"name": "Q1 Planning", "dataSourceType": "dynamic"}`).
2. **Validation**: Pydantic parses the JSON, applies aliases to map camelCase → snake_case, validates field types and patterns (e.g., visibility must be private/workspace/public).
3. **Persistence**: Beanie ODM serializes the validated model to BSON and writes to MongoDB's `pockets` collection. Timestamps are automatically set.
4. **Retrieval**: Queries via workspace index are fast. Widgets are returned as embedded documents within the Pocket.
5. **Updates**: Widget updates can target specific widgets by ID without affecting others or the array index.

### Control Flow Example: Creating a Widget in a Pocket

```
User Action (Frontend)
  ↓
API Router receives POST /pockets/{pocket_id}/widgets
  ↓
Pocket Service validates widget data as Widget model
  ↓
Widget is appended to pocket.widgets list
  ↓
Pocket document is saved (all widgets serialized)
  ↓
Event Handler (e.g., on_pocket_updated) may trigger downstream actions
```

### Edge Cases

- **Widget ID Collisions**: Extremely unlikely (ObjectId-based), but if a Widget is created without an explicit `id`, a new one is generated. Duplicates would be caught at the API layer.
- **Team/Agent Polymorphism**: Since `team` and `agents` accept `Any`, downstream services must handle both scalar IDs and populated objects. Consider using a discriminated union or strict type validation at the service layer.
- **Ripple Spec Flexibility**: The schema doesn't validate `rippleSpec` content, delegating validation to the ripple/workflow service.
- **Visibility vs. Shared Access**: A Pocket can be "private" but still have users in `shared_with`. The authorization layer (not this module) must decide which takes precedence.

## Authorization and Security

This module **does not enforce authorization**; it only defines the data model. Authorization is handled elsewhere (likely in the API router or a middleware layer). However, the schema supports these access patterns:

- **Visibility Enum**: Defines default scope (private to owner, workspace to team, public to anyone)
- **Owner Field**: The creating user; typically has full permissions
- **Shared With List**: Explicit user IDs with granted access, overriding visibility
- **Share Link Token**: Anonymous access via token (useful for public dashboards)
- **Share Link Access**: Granular permission for link sharers (view-only, comment, edit)

**Who Can Modify a Pocket**:
Typically the owner or users in `shared_with` with "edit" access. The pocket service layer validates this before mutation.

## Dependencies and Integration

### Inbound Dependencies

**What depends on this module**:

| Dependent | Usage | Reason |
|-----------|-------|--------|
| `ee.cloud.models.__init__` | Re-exports Pocket, Widget, WidgetPosition | Makes models available to the package |
| `pocket_service` | CRUD operations on Pocket documents | Queries, creates, updates, deletes Pockets and Widgets |
| `event_handlers` | Listens to Pocket lifecycle events | Triggers downstream actions (notifications, ripple execution, etc.) when Pockets/Widgets change |
| API routers | Request/response serialization | Converts HTTP JSON ↔ Pocket/Widget models |

### Outbound Dependencies

**What this module depends on**:

| Dependency | From | Purpose |
|------------|------|----------|
| `TimestampedDocument` | `ee.cloud.models.base` | Base class providing `created_at` and `updated_at` fields and MongoDB integration |
| `Beanie` | beanie | ODM (Object-Document Mapper) for MongoDB; `Indexed` for efficient queries |
| `Pydantic` | pydantic | Data validation, serialization, field aliases |
| `ObjectId` | bson | BSON MongoDB ID generation |

### Integration Pattern

The module is a **schema definition layer** that sits between the database (MongoDB) and business logic (service layer). It's consumed by:
- **Service Layer**: Uses Pocket/Widget models for typed method signatures
- **API Layer**: Deserializes requests into models, serialializes responses
- **Event Handlers**: Receives model instances when documents change

---

## Design Decisions

### 1. Widget ID Independence
**Decision**: Widgets have their own `id` field instead of being addressed by array index.

**Rationale**:
- Frontend widgets are often reordered on the UI; using indices would break references
- IDs allow direct widget updates without reloading the entire Pocket
- Mirrors REST best practices (each resource has an ID)

---

### 2. Field Aliases for Frontend Compatibility
**Decision**: camelCase aliases (e.g., `dataSourceType` → Python `dataSourceType` with alias `"dataSourceType"`) coexist with snake_case Python field names.

**Rationale**:
- JavaScript frontend sends/expects camelCase (convention)
- Python backend prefers snake_case (PEP 8)
- Pydantic's `populate_by_name = True` lets both work seamlessly
- No manual marshaling needed

---

### 3. Flexible Team/Agent References
**Decision**: `team` and `agents` fields accept `list[Any]` rather than `list[str]` or `list[ObjectId]`.

**Rationale**:
- Supports both lazy loading (store IDs) and eager loading (populate full objects)
- Reduces database round-trips if team/agent data is needed immediately
- Trade-off: Less type safety; requires downstream validation

---

### 4. Optional Ripple Spec
**Decision**: `rippleSpec` is a loose `dict[str, Any] | None`, not a strict schema.

**Rationale**:
- Ripple feature is evolving; tight coupling would require schema migrations
- Allows Pocket service to store ripple data without understanding it
- Ripple service owns validation and interpretation

---

### 5. Visibility Enum + Explicit Sharing
**Decision**: Combines a default visibility level (private/workspace/public) with an explicit `shared_with` list.

**Rationale**:
- Visibility covers common cases (keep it private by default)
- Explicit list allows fine-grained control without creating many visibility levels
- Trade-off: Authorization logic must handle precedence rules (Does "public" override `shared_with`? etc.)

---

### 6. Share Link Separation
**Decision**: Share links are represented as a token + access level, not as users in `shared_with`.

**Rationale**:
- Links can be revoked without tracking who used them
- Anonymous access doesn't require user accounts
- Different permission model (view-only for links, edit for team members)

---

## Related

- [base-foundational-document-model-with-automatic-timestamp-management-for-mongodb](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md)
- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
- [untitled](untitled.md)
