# comment — Threaded comments on pockets and widgets with workspace isolation

> This module defines the data models for a collaborative commenting system that enables threaded discussions on pockets (content containers) and widgets within a workspace. It exists to provide a structured, queryable representation of comments with support for mentions, resolution status, and hierarchical replies. The module serves as the persistence layer for collaborative feedback and discussion features in the PocketPaw platform.

**Categories:** Data Model / Persistence, Collaboration Features, Multi-tenant Architecture, Core Domain Model  
**Concepts:** Comment, CommentTarget, CommentAuthor, TimestampedDocument, Threaded comments, Polymorphic targeting, Workspace scoping, Multi-tenant isolation, Immutable snapshots, Beanie ODM  
**Words:** 1567 | **Version:** 1

---

## Purpose

The `comment` module provides domain models for a **threaded commenting system** in PocketPaw, enabling users to collaborate through inline discussions. Unlike simple flat comments, this system supports:

- **Hierarchical threads**: Comments can be replies to other comments (via the `thread` field), creating conversation branches
- **Multi-target commenting**: Comments can be attached to pockets, widgets, or agents via a flexible `CommentTarget` structure
- **Workspace isolation**: Comments are scoped to workspaces, ensuring data boundaries in multi-tenant environments
- **User mentions**: The `mentions` field tracks @-mentions for notifications and visibility
- **Resolution workflows**: Comments can be marked as resolved with audit trails (`resolved_by`), supporting issue-tracking patterns

This module exists separately because commenting is a **cross-cutting concern** that appears in multiple feature areas (pockets, widgets, agents) and requires consistent handling. By centralizing the data model, the system ensures uniform behavior for comment creation, querying, and lifecycle management across all target types.

## Key Classes and Methods

### CommentTarget(BaseModel)

**Purpose**: Encapsulates the location where a comment is attached, supporting polymorphic targeting of different entity types.

**Fields**:
- `type: str` — Enum-like field (pattern `"^(pocket|widget|agent)$"`) indicating the target entity type. This drives different business logic in consuming services (e.g., pocket comments vs. widget comments may have different permission models).
- `pocket_id: str` — Always required; the pocket containing the target. Even widget comments reference their parent pocket for workspace-level scoping.
- `widget_id: str | None` — Optional; specified only when the comment targets a widget within a pocket. A None value indicates a pocket-level comment.

**Business logic**: This design enforces that all comments exist within a pocket context, simplifying queries like "all comments in pocket X" without requiring joins. The optional `widget_id` allows granular targeting without forcing a separate table.

### CommentAuthor(BaseModel)

**Purpose**: Immutable snapshot of the comment author at creation time, preserving author information even if the user is later deleted or renamed.

**Fields**:
- `id: str` — User identifier (typically maps to a User document ID)
- `name: str` — Display name at time of commenting
- `avatar: str` — Avatar URL or embedded image reference (defaults to empty string for users without avatars)

**Business logic**: Storing author as a nested object rather than a reference means the UI can render "Alice commented" even if Alice's profile is later deleted. This is a common pattern in collaborative systems to maintain comment readability.

### Comment(TimestampedDocument)

**Purpose**: The primary data model representing a single comment in the system, with full lifecycle metadata.

**Inheritance**: Extends `TimestampedDocument` (from `ee.cloud.models.base`), providing `created_at` and `updated_at` timestamps automatically.

**Key fields**:
- `workspace: Indexed(str)` — Workspace identifier, indexed for efficient filtering. All queries will include `workspace` in their predicates to enforce multi-tenant isolation.
- `target: CommentTarget` — Where this comment is attached (pocket, widget, or agent)
- `thread: str | None` — Parent comment ID if this is a reply; None for root-level comments. Creates the threaded hierarchy.
- `author: CommentAuthor` — Who wrote this comment (immutable snapshot)
- `body: str` — Comment text content; no length limit enforced at model level (validation likely in service layer)
- `mentions: list[str]` — List of user IDs mentioned via @-mention syntax; used for notification triggers
- `resolved: bool` — Whether this comment/issue has been addressed (defaults to False)
- `resolved_by: str | None` — User ID who marked it resolved (audit trail)

**Database settings**:
```python
class Settings:
    name = "comments"  # Collection name in MongoDB
    indexes = [
        [(("target.pocket_id", 1), ("created_at", -1))]
    ]
```

The compound index on `(target.pocket_id, created_at)` optimizes the common query pattern: "fetch all comments for pocket X, sorted newest first." The descending order on `created_at` avoids additional sorting overhead.

## How It Works

### Data Flow

1. **Comment Creation**: When a user submits a comment, a consuming service (e.g., `CommentService` or an API route) creates a `Comment` instance with:
   - Current user's ID/name/avatar → `author`
   - Current workspace ID → `workspace`
   - Target coordinates (pocket_id, widget_id or agent type) → `target`
   - User-supplied text → `body`
   - Parsed @-mentions → `mentions`
   - No `thread` (or optional parent comment ID if replying)
   - `resolved = False` initially

2. **Storage**: Beanie ORM persists the document to MongoDB's `comments` collection, generating `_id` and timestamps.

3. **Retrieval patterns**:
   - **Comments on a pocket**: `Comment.find(Comment.target.pocket_id == "pocket_123", Comment.workspace == "ws_456").sort(("created_at", -1))` — uses the indexed field
   - **Thread replies**: `Comment.find(Comment.thread == "comment_parent_id")` — fetches all replies to a specific comment
   - **User mentions**: `Comment.find(Comment.mentions.contains("user_789"))` — for notification systems

4. **Resolution workflow**: When an issue comment is resolved, a service updates the document:
   ```python
   comment.resolved = True
   comment.resolved_by = current_user_id
   await comment.save()  # Triggers updated_at update via TimestampedDocument
   ```

### Edge Cases

- **Deleted users**: Author snapshot preserves the name/avatar; `mentions` references may point to non-existent users (services must handle gracefully)
- **Deeply nested threads**: No depth limit is enforced; clients should implement UI truncation (e.g., show only 2 levels, "load more" for deeper replies)
- **Empty mentions**: `mentions` defaults to empty list; no validation prevents posting comments with body-text mentions that aren't in the list
- **Widget comments without widget_id**: Model allows `widget_id = None` but `type = "widget"`, creating ambiguous state (validation likely in service layer)

## Authorization and Security

This model layer does **not enforce authorization**; that responsibility belongs to consuming services (API routers or service classes). Typical patterns:

- **Read**: Users can see comments in workspaces they're members of
- **Create**: Users must be workspace members; rate-limiting likely applied in service layer
- **Resolve**: Typically restricted to comment author, pocket owner, or workspace admins
- **Delete**: Often restricted to author or admins; soft-delete pattern may be used (not visible in this model)

The `workspace` field is the **isolation boundary**—queries should always filter by workspace to prevent cross-workspace leakage. This is a responsibility of the consuming service/repository layer.

## Dependencies and Integration

### Incoming dependencies (what imports this module)

- `__init__` (package-level exports) — Makes `Comment`, `CommentTarget`, `CommentAuthor` available to other modules
- Implicit consumers: API routes, services, and tests that need to instantiate or query comments

### Outgoing dependencies (what this module imports)

- **`ee.cloud.models.base.TimestampedDocument`** — Base class providing `created_at` and `updated_at` automatic timestamps. This is a shared base used across PocketPaw documents, ensuring consistent timestamp handling.
- **`beanie.Indexed`** — ODM decorator marking fields for database indexing. Beanie is the async MongoDB ORM layer.
- **`pydantic.BaseModel`, `pydantic.Field`** — Validation and serialization; BaseModel defines `CommentTarget` and `CommentAuthor` as simple nested structures with schema validation.

### Downstream integration patterns

- **CommentService** (likely exists in service layer) — CRUD operations, thread resolution, mention parsing
- **Comment API routes** — FastAPI endpoints for POST (create), GET (list by pocket), PUT (resolve)
- **Notification system** — Subscribes to comment creation events; queries `mentions` to trigger alerts
- **Search/indexing** — May replicate comment data to Elasticsearch for full-text search

## Design Decisions

### 1. **Immutable author snapshot vs. user reference**
- **Choice**: Store author as nested `CommentAuthor` (name, avatar) rather than just `author_id`
- **Rationale**: Comments remain human-readable even after user deletion/rename. Immutability preserves historical accuracy ("Alice said..." not "User#123 said...")
- **Trade-off**: If a user updates their avatar, old comments won't reflect it (acceptable in collaborative tools)

### 2. **Workspace as indexed field**
- **Choice**: Every `Comment` has an explicit `workspace` field, indexed
- **Rationale**: Multi-tenant SaaS requirement; enables efficient per-workspace queries without relying on workspace ID from request context
- **Trade-off**: Denormalizes the pocket → workspace relationship (pocket documents would already contain workspace); justified because comments are frequently queried in isolation

### 3. **Flexible CommentTarget with type enum**
- **Choice**: Single `target` field with `type`, `pocket_id`, optional `widget_id` rather than separate Comment subclasses
- **Rationale**: All comment queries and operations are identical regardless of target type; polymorphism via type field is simpler than document inheritance
- **Trade-off**: No database-level enforcement of "if type=widget, widget_id must be non-null" (validation is application-level responsibility)

### 4. **Simple thread model with parent_id**
- **Choice**: `thread: str | None` points to a parent comment; no separate ThreadGroup model
- **Rationale**: Threads are shallow in practice (1-2 levels); parent-id is simpler to query and index than a separate collection
- **Trade-off**: Deep nesting (replies to replies) requires client-side recursion or multiple queries; not optimized for very deep discussions

### 5. **Mentions as list of user IDs, not parsed objects**
- **Choice**: Store `mentions: list[str]` (raw IDs) rather than full user objects or parsed mention ranges
- **Rationale**: Minimal storage; enables efficient queries ("notify these users") without maintaining mention object state
- **Trade-off**: Clients must parse `body` text independently to render mentions; no shared mention syntax validation at model level

### 6. **Single compound index strategy**
- **Choice**: One index on `(target.pocket_id, created_at)` instead of multiple indexes
- **Rationale**: The dominant query pattern is "comments on pocket X sorted by recency"; one well-chosen index beats many small ones
- **Trade-off**: Queries on `workspace` alone or `mentions` may be slower; acceptable because these are secondary query patterns

## Architectural Context

This module is part of PocketPaw's **collaboration layer**, sitting between:
- **Domain models** (above): API schemas, service DTOs that may reshape comments for API responses
- **Persistence layer** (below): Beanie ORM, MongoDB driver, database indexes

It represents a **clean separation** of concerns:
- Model = what the data looks like (schema, validation, indexed fields)
- Service = how comments behave (threaded logic, mention resolution, permissions)
- API = how clients interact with comments (REST or GraphQL endpoints)

This separation allows the schema to evolve independently of the API contract.

---

## Related

- [base-foundational-document-model-with-automatic-timestamp-management-for-mongodb](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md)
- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
