# session — Cloud-tracked chat session document model for pocket-scoped conversations

> The session module defines the Session document model that represents individual chat conversations in the PocketPaw system. It exists to provide a persistent, queryable data structure for tracking chat metadata (ownership, workspace affiliation, activity) while messages themselves are stored separately in Python memory. This module bridges the frontend UI contract (camelCase field naming) with the backend storage layer, enabling efficient session discovery and filtering across workspaces and organizational units.

**Categories:** chat / messaging, data model / ORM, workspace management, MongoDB persistence  
**Concepts:** Session (class), TimestampedDocument (inheritance), Beanie ODM, MongoDB document model, Indexed fields, Unique constraints, Composite indexes, Soft deletion, Pydantic model, Field aliases  
**Words:** 1694 | **Version:** 1

---

## Purpose

The session module solves the core data modeling problem for PocketPaw's chat system: how to track and organize conversations at scale. Each Session document represents a single chat conversation with metadata about who created it, where it lives (pocket/group/agent), when it was last active, and statistics like message count.

This module exists because:

1. **Metadata separation**: Messages are stored in Python memory for performance, but metadata needs persistent, queryable storage in MongoDB for discovery, history, and multi-instance coordination.
2. **Frontend contract alignment**: The field naming uses camelCase with explicit aliases to match the JavaScript/frontend API contract, ensuring seamless serialization without transformation layers.
3. **Multi-tenant scoping**: Sessions must be efficiently filtered by workspace and owned by users, requiring indexed fields for performant queries.
4. **Soft deletion support**: The `deleted_at` field enables logical deletion without losing historical records, important for audit trails and recovery.

In the system architecture, Session is a **data model layer** component that sits between:
- **Upward**: Frontend clients that query/create sessions and display chat history
- **Downward**: MongoDB via Beanie ODM for persistence
- **Sideways**: Service layer components (imported by `ee.cloud.models.service`) that implement business logic around session CRUD and filtering

## Key Classes and Methods

### Session (class)

**Purpose**: Represents a single chat session document with complete metadata for tracking, ownership, and organizational context.

**Key Fields**:

- `sessionId: Indexed(str, unique=True)` — Unique identifier for the session, guaranteed distinct across the system. The `Indexed` and `unique=True` parameters ensure database-level uniqueness constraints.
- `pocket: str | None` — The pocket (personal/private area) this session belongs to, if any. Nullable because sessions can be group or agent-scoped instead.
- `group: str | None` — Group identifier if this is a group conversation, mutually exclusive with pocket/agent in typical usage.
- `agent: str | None` — Agent identifier if this session is tied to a specific agent/bot, optional organizational unit.
- `workspace: Indexed(str)` — The workspace ID, required and indexed for tenant isolation. All queries are scoped to workspace.
- `owner: str` — User ID of the session creator, no default. Enables permission checks and ownership filtering.
- `title: str` — Human-readable session name, defaults to "New Chat" if not provided.
- `lastActivity: datetime` — Timestamp of the most recent activity in the session, automatically set to current UTC time on creation. Used for sorting and "recent conversations" UIs.
- `messageCount: int` — Counter tracking total messages in the session, defaults to 0. Incremented by service layer when messages are added.
- `deleted_at: datetime | None` — Soft-delete timestamp. If populated, the session is logically deleted. Query filters typically exclude sessions where `deleted_at` is not None.

**Inherited Behavior** (from `TimestampedDocument`):
- `created_at: datetime` — Automatically set when document is created
- `updated_at: datetime` — Automatically updated on any field modification
- `_id: ObjectId` — MongoDB default primary key

**Configuration**:

- `model_config = {"populate_by_name": True}` — Allows both the field name (`lastActivity`) and alias (`lastActivity`) to be accepted in JSON input/output, important for backward compatibility and client flexibility.
- `Settings.name = "sessions"` — Maps the Pydantic model to the MongoDB collection named "sessions".
- `Settings.indexes` — Two composite indexes:
  1. `[("workspace", 1), ("pocket", 1), ("lastActivity", -1)]` — For finding recent sessions within a pocket; ascending workspace + pocket, descending last activity for natural "most recent first" ordering.
  2. `[("workspace", 1), ("group", 1), ("agent", 1)]` — For finding sessions by group/agent within workspace; useful for filtering conversations by organizational unit.

These indexes are critical for query performance; without them, filtering across thousands of sessions would be slow.

## How It Works

### Data Flow

1. **Creation**: A service layer endpoint (or client) calls the repository to create a new Session. Pydantic validates all fields. `lastActivity` defaults to now in UTC if not provided. The document is inserted into MongoDB.

2. **Querying**: Service methods retrieve sessions using the indexed fields:
   - "Show me the 10 most recent sessions in workspace W and pocket P" → Uses index 1 with workspace + pocket filters, ordered by lastActivity descending.
   - "Show me all sessions in group G" → Uses index 2 with workspace + group filters.
   - "Find session by ID" → Uses `sessionId` unique index.

3. **Updates**: When a message is added to a session (in Python memory), the service layer increments `messageCount` and updates `lastActivity` to current time. The `updated_at` field is auto-bumped by Beanie.

4. **Soft Deletion**: Instead of removing the document, service sets `deleted_at = datetime.now(UTC)`. Query filters add `deleted_at: None` condition to hide deleted sessions.

### Edge Cases

- **Null pocket/group/agent**: A session can be tied to a workspace + owner only, with all three of these fields None. Service queries must handle this carefully—don't assume one will always be populated.
- **messageCount out of sync**: If the Python message store crashes or loses data, `messageCount` on the Session document may no longer match reality. Service layer should consider this a metadata cache, not the source of truth.
- **lastActivity not updated**: If service layer forgets to update `lastActivity` when adding a message, sorting by "recent" will show stale data. Callers should depend on this being kept in sync.
- **Timezone handling**: The `Field(default_factory=lambda: datetime.now(UTC))` ensures UTC timezone awareness, avoiding ambiguity and daylight saving issues.

## Authorization and Security

This module defines the data structure; authorization is enforced at the service/endpoint layer:

- **Workspace isolation**: All queries should filter by `workspace` to prevent cross-tenant data leakage. A service function querying sessions without a workspace filter is a security bug.
- **Owner verification**: Endpoints should check that the requesting user matches `owner` (or has admin/group permission) before returning or modifying a session.
- **Soft delete privacy**: Queries must filter `deleted_at: None` unless the user has auditing/admin privileges.

The model itself does not enforce these; it is the responsibility of the service layer (imported by `ee.cloud.models.service`) to apply these rules.

## Dependencies and Integration

### Imports

- **base** (`ee.cloud.models.base.TimestampedDocument`) — Parent class providing `created_at`, `updated_at` fields and Beanie ODM integration. Session extends this to inherit automatic timestamp management.
- **beanie** (`Indexed`) — ODM (Object-Document Mapper) for MongoDB integration. `Indexed(str, unique=True)` tells MongoDB to create a unique index on `sessionId`.
- **pydantic** (`Field`) — Defines field metadata like aliases and defaults. `alias="sessionId"` maps the Python field name to JSON key names.
- **datetime** (`UTC`) — Standard library datetime utilities for timezone-aware timestamps.

### Imported By

- **`__init__`** (package initializer) — Likely re-exports Session for public API visibility, so callers use `from ee.cloud.models import Session` rather than the full path.
- **`service`** (`ee.cloud.models.service` or `ee.cloud.service`) — Business logic layer that performs CRUD operations on Session documents, implements filtering, updates messageCount, manages soft deletes, and enforces authorization.

### System Integration

- **Frontend clients** → POST `/sessions` with workspace, pocket, title → Service layer creates Session → Returns document with sessionId to client.
- **Message ingestion** → Client sends message → Service adds to Python message store, increments Session.messageCount, updates Session.lastActivity → MongoDB persistence.
- **Session discovery** → Client requests "show recent sessions" → Service queries using index 1 (workspace + pocket + lastActivity) → Returns sorted list.
- **Workspace deletion** → Admin deletes workspace W → Service queries all sessions with workspace=W and soft-deletes them (sets deleted_at).

## Design Decisions

### 1. **Metadata in MongoDB, Messages in Python**

Sessions metadata (timestamps, count, ownership) lives in MongoDB for durability and queryability. Messages are kept in Python process memory (presumably in-memory cache or separate storage). This separation trades off consistency (message count may drift) for:
- **Query performance**: Session list queries hit MongoDB indexes, not slower message stores.
- **Reduced database load**: Messages are often voluminous; storing only metadata keeps the collection lean.
- **Flexibility**: Message storage can be changed (Redis, S3, file system) without altering Session schema.

### 2. **camelCase Aliases for Frontend Contract**

Fields like `lastActivity` have `alias="lastActivity"` (the field and alias are identical here, but the pattern shows intent). The `populate_by_name = True` config allows both the Python name and JSON alias to work. This is intentional coupling to the frontend:
- **Pro**: No transformation layer needed; frontend sends `{"lastActivity": "..."}` and Pydantic maps it directly.
- **Con**: Changing field names requires frontend coordination. The comment "Field names use camelCase aliases to match the frontend contract" signals this is intentional.

### 3. **Soft Deletes with `deleted_at`

Instead of removing documents, sessions are marked deleted with a timestamp. Benefits:
- **Recoverability**: Admins can restore deleted sessions.
- **Audit trail**: Preserves "who deleted when" for compliance.
- **Query safety**: Default filters exclude `deleted_at IS NOT NULL`, reducing chance of accidental exposure.

Trade-off: Queries must always include the `deleted_at: None` filter, or garbage collection is needed periodically.

### 4. **Composite Indexes for Access Patterns**

Two indexes reflect expected query patterns:
- Index 1: Workspace + pocket + recent activity = "show my recent chats in my pocket"
- Index 2: Workspace + group + agent = "show all conversations in this group/agent"

These are not exhaustive; other queries (e.g., by owner, by agent alone) may not be optimized. Service layer should document which queries are O(log N) vs O(N).

### 5. **Indexed(str, unique=True) for sessionId**

The `sessionId` is unique cluster-wide. This could be a UUID, nanoid, or similar. The uniqueness constraint prevents accidental duplicates and enables foreign key references from message documents. Important assumption: sessionId generation is centralized and deterministic (e.g., a service method, not scattered clients).

### 6. **Nullable Pocket/Group/Agent**

These three fields are optional and likely mutually exclusive in practice (a session is scoped to one organizational unit). However, the schema allows all three to be None or any combination to be set. Service layer logic should validate the intended constraint (e.g., exactly one of {pocket, group, agent} is set), not the schema.

---

## Migration and Future Considerations

- **Message storage relocation**: If messages move from Python to a separate store (Firestore, Redis), the messageCount field becomes a cache that needs invalidation strategy.
- **Multi-tenant scale**: At 10M+ sessions per workspace, the composite indexes may need refinement or sharding by workspace.
- **Session archival**: Very old sessions (>1 year) could be archived to cold storage; the soft-delete pattern supports this.
- **Read replicas**: Queries can be routed to read replicas; writes (create, update, soft-delete) must hit the primary.

---

## Related

- [base-foundational-document-model-with-automatic-timestamp-management-for-mongodb](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md)
- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
- [untitled](untitled.md)
