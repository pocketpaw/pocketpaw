# notification — In-app notification data model and persistence for user workspace events

> This module defines the data models for in-app notifications that inform users about workspace events (mentions, comments, replies, invites, agent completions, and shared pockets). It exists as a dedicated model layer to provide a clean, reusable schema for notification storage and querying, enabling the event system to persist user-facing notifications independently of transactional events. Notifications are workspace-scoped, recipient-indexed, and support lifecycle management (read status, expiration).

**Categories:** Data Model / Persistence, Notification / User Communication, Workspace / Multi-tenancy, Event-Driven Architecture  
**Concepts:** Notification, NotificationSource, TimestampedDocument, in-app notifications, workspace scoping, recipient indexing, soft delete pattern, expiration lifecycle, Beanie ODM, MongoDB indexing  
**Words:** 1487 | **Version:** 1

---

## Purpose

The notification module exists to provide a persistent, queryable representation of in-app notifications delivered to users. While events (handled elsewhere in the system) represent what happened in the system, notifications represent *communications about those events to specific users*.

**Why separate?** Notifications have distinct concerns:
- **Storage requirements**: Notifications must be queryable by recipient and read status for inbox-style UIs
- **Lifecycle management**: Notifications can expire, be marked read, or be dismissed—different from immutable events
- **Workspace scoping**: Notifications are workspace-isolated resources, unlike some transactional events
- **Performance**: A user may generate thousands of events; notifications are a smaller, intentionally curated subset

**System role**: This module sits at the data model layer, consumed by event handlers that translate system events into user notifications. The `event_handlers` module imports this to create notifications when meaningful events occur; the `__init__` re-exports it for clean public API.

## Key Classes and Methods

### `NotificationSource(BaseModel)`

A lightweight Pydantic model that captures *where* a notification originated—the resource that triggered it.

**Fields:**
- `type: str` — The resource type (e.g., "pocket", "comment", "invite") that triggered the notification
- `id: str` — The identifier of that resource
- `pocket_id: str | None` — Optional reference to a parent pocket, for nested-resource contexts (a comment within a pocket)

**Purpose**: Provides a back-reference so users can navigate from a notification to its source. Unlike storing raw IDs scattered across the Notification schema, this encapsulates the source as a cohesive unit. The optional `pocket_id` handles cases where the source is already within a pocket context.

### `Notification(TimestampedDocument)`

The primary notification persistence model, extending `TimestampedDocument` (which provides `created_at` and `updated_at` timestamps via the base class).

**Core Fields:**
- `workspace: Indexed(str)` — Workspace ID; indexed for tenant isolation. Ensures notifications are workspace-scoped, preventing cross-workspace leakage.
- `recipient: Indexed(str)` — User ID receiving the notification; indexed for fast inbox queries ("get all my notifications").
- `type: str` — Notification category: "mention", "comment", "reply", "invite", "agent_complete", or "pocket_shared". Drives UI rendering logic (different icons/colors per type).
- `title: str` — Short, human-readable summary (e.g., "John mentioned you in a comment").
- `body: str` — Optional longer description or context.
- `source: NotificationSource | None` — Backreference to the triggering resource. Optional because some notifications may be system-generated without a specific source.
- `read: bool = False` — Soft read state. Notifications are not deleted, only marked read. Enables "undo" semantics and analytics.
- `expires_at: datetime | None` — Optional expiration timestamp. Notifications can auto-expire (e.g., time-limited invites). Queries can filter `expires_at > now()` to hide expired notifications.

**Database Settings:**
```python
class Settings:
    name = "notifications"  # MongoDB collection name
    indexes = [
        [('recipient', 1), ('read', 1), ('created_at', -1)]
    ]
```
The composite index optimizes the common query pattern: *"Get unread notifications for user X, sorted by recency."* This is the inbox query performed on every app load. The index enables efficient filtering by recipient and read status, then sorts by creation time descending (newest first).

## How It Works

### Notification Lifecycle

1. **Creation (Event Handler)**: When a system event occurs (e.g., a user mentions another user in a comment), the `event_handlers` module intercepts it and calls `Notification.insert()` with appropriate fields. The handler translates domain events into user-facing notification semantics.

2. **Storage**: Beanie ODM persists the document to MongoDB's `notifications` collection. Timestamps (`created_at`, `updated_at`) are set automatically by `TimestampedDocument`.

3. **Querying**: The inbox UI queries: `Notification.find(recipient=user_id, read=False).sort('created_at', -1)`. The composite index makes this efficient.

4. **User Interaction**:
   - **Mark as read**: `Notification.update(read=True)` (typically bulk-updated)
   - **Expire**: System daemon or query filter excludes notifications where `expires_at < now()`
   - **Navigate to source**: UI extracts `notification.source.type` and `notification.source.id` to navigate user to the comment/pocket/invite.

5. **Retention**: Notifications are not hard-deleted; old read notifications remain in the database for audit/analytics. Admin retention policies may soft-delete (mark with a deleted flag) or archive in a separate collection.

### Data Flow Example

```
Event: User A comments in pocket P, mentioning User B
  ↓
Event Handler (event_handlers module)
  ├─ Recognizes @mention pattern
  ├─ Translates to Notification document:
  │   {
  │     workspace: "workspace_123",
  │     recipient: "user_b_id",
  │     type: "mention",
  │     title: "Alice mentioned you",
  │     body: "in pocket 'Project Plan'",
  │     source: { type: "comment", id: "comment_789", pocket_id: "pocket_456" },
  │     read: false,
  │     created_at: "2024-01-15T10:30:00Z"
  │   }
  │   ↓
  └─ Calls Notification.insert()
        ↓
        MongoDB stores document
        ↓
User B opens app
  ├─ UI queries: Notification.find({recipient: "user_b_id", read: false})
  ├─ Displays list ("Alice mentioned you in Project Plan")
  └─ On click: extracts source → navigates to comment_789
```

### Edge Cases

- **Duplicate notifications**: If the same event handler fires twice, two identical notifications are created. Idempotency is the event handler's responsibility, not this model's.
- **Null source**: Some notifications (e.g., "Welcome to workspace") have no actionable source; `source` is optional.
- **Expired but unread**: A notification can expire and remain unread. The UI should hide it (via `expires_at` filter) even if `read=false`.
- **Timezone awareness**: `expires_at` is a `datetime` object. Callers must ensure it's timezone-aware (UTC recommended) for correct comparisons.

## Authorization and Security

This module does not enforce authorization directly; it's a data model, not a service. Authorization is enforced at the API/handler layer:

- **Workspace isolation**: Any query must include `workspace=current_workspace_id` to prevent cross-workspace reads. The model enforces this via schema, but callers are responsible for including it.
- **Recipient access**: Only the recipient (or workspace admins) should be able to read/update a notification. This is enforced in the service/API layer that uses this model, not here.
- **Read status updates**: Only the recipient can mark their own notifications as read. Again, enforced upstream.

The indexed `recipient` field enables efficient access control checks ("does user own this notification?").

## Dependencies and Integration

### Imports

- **`beanie.Indexed`**: ODM decorator for MongoDB indexing. Signals that `workspace` and `recipient` are index-participating fields for efficient queries.
- **`ee.cloud.models.base.TimestampedDocument`**: Base class providing `created_at` and `updated_at` fields. Ensures all notifications have creation/update timestamps without boilerplate.
- **`pydantic.BaseModel`, `pydantic.Field`**: Data validation and schema definition. `NotificationSource` uses BaseModel directly for nested validation.

### Exported To

- **`event_handlers`**: Imports `Notification` to instantiate and persist notifications when events occur. This is the primary consumer.
- **`__init__` (cloud.models)**: Re-exports for clean public API (`from ee.cloud.models import Notification`).

### Relationship to Other Models

- **Events** (elsewhere in codebase): Events are immutable, system-wide records. Notifications are mutable (read status), user-scoped derivatives of events.
- **User/Workspace models**: Notifications reference these by ID (`recipient`, `workspace`) but do not embed them (no foreign key relationships in MongoDB). The caller is responsible for ensuring referential integrity.
- **Comment/Pocket/Invite models**: Referenced indirectly via `NotificationSource.id`. No direct dependency here; event handlers perform the translation.

## Design Decisions

### 1. **Soft Read State vs. Deletion**
**Decision**: Notifications are marked `read: bool` rather than deleted when read.

**Rationale**:
- Preserves notification history for user reference ("did I already see this?")
- Enables notification badges ("5 unread notifications")
- Supports undo/restore workflows
- Provides analytics data (when did user read what?)
- Avoids hard deletes, which complicate recovery and auditing

### 2. **Optional Expiration**
**Decision**: `expires_at: datetime | None` is optional and must be explicitly checked in queries.

**Rationale**:
- Most notifications are perpetual; optional field avoids clutter
- Expiration logic lives in the query layer, not the model (read-only concern)
- Flexibility: invitations may expire, but mention notifications don't
- Trade-off: Callers must remember to filter expired notifications; no automatic hiding

### 3. **Composite Index on (recipient, read, created_at)**
**Decision**: Single three-field index rather than separate indexes or two-field variants.

**Rationale**:
- Optimizes the dominant query: "unread notifications for user X, sorted by time"
- (recipient, read) filters the set quickly; (created_at, -1) sorts within it
- Avoids index explosion for a small model
- Trade-off: Queries on other field combinations (e.g., just recipient) still benefit but with secondary sort

### 4. **Nested NotificationSource Model**
**Decision**: `source` is a separate Pydantic model, not a flat set of fields.

**Rationale**:
- Encapsulation: source information is cohesive
- Reusability: if other models need to reference a resource, they can use `NotificationSource`
- Validation: Pydantic validates source structure at insertion
- Trade-off: Slightly more verbose than flat fields; worth it for clarity

### 5. **No Explicit User/Workspace Validation**
**Decision**: `workspace` and `recipient` are strings; no validation against user/workspace documents.

**Rationale**:
- MongoDB is schemaless; validation would require additional queries
- In a distributed system, referential integrity is better handled by event handlers (which create notifications and can verify source existence)
- Avoids tight coupling between models
- Trade-off: Orphaned notifications are possible if a user is deleted; handled via cleanup jobs, not model logic

## Common Query Patterns

**Get inbox (unread notifications for a user):**
```python
await Notification.find(
    Notification.workspace == workspace_id,
    Notification.recipient == user_id,
    Notification.read == False,
    Notification.expires_at == None | (Notification.expires_at > datetime.now())
).sort([("created_at", -1)]).to_list()
```

**Mark notifications as read:**
```python
await Notification.find(
    Notification.recipient == user_id,
    Notification.read == False
).update({"$set": {"read": True}})
```

**Get all notifications (including read) for user:**
```python
await Notification.find(Notification.recipient == user_id).sort([("created_at", -1)]).to_list()
```

---

## Related

- [base-foundational-document-model-with-automatic-timestamp-management-for-mongodb](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md)
- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
- [untitled](untitled.md)
