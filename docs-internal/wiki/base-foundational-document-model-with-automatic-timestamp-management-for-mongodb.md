# base — Foundational document model with automatic timestamp management for MongoDB persistence

> This module provides `TimestampedDocument`, a base class that extends Beanie's ODM `Document` to automatically manage `createdAt` and `updatedAt` timestamps on all database operations. It exists to eliminate boilerplate timestamp logic across the system and ensure consistent, UTC-based audit trails on all domain entities. It serves as the architectural foundation for all entity models in the pocketPaw system (agents, messages, workspaces, etc.), enabling automatic temporal tracking without requiring downstream classes to implement timestamp logic.

**Categories:** data model layer, temporal auditing, MongoDB persistence, foundational infrastructure, cross-cutting concerns  
**Concepts:** TimestampedDocument, createdAt, updatedAt, Beanie ODM, before_event decorator, Insert event, Replace event, Save event, Update event, UTC timezone  
**Words:** 1441 | **Version:** 1

---

## Purpose

The `base` module solves a fundamental data modeling problem: maintaining reliable, consistent audit timestamps across all entities in a MongoDB-backed system. Rather than requiring every model class to implement timestamp management independently, this module provides a reusable base class that automatically captures when documents are created and modified.

**Why it exists as a separate module:**
- **DRY principle**: Prevents timestamp logic duplication across 7+ entity models (agent, comment, group, message, notification, pocket, session, workspace)
- **Centralized audit trail strategy**: Ensures all entities follow identical timestamp semantics (UTC-based, always maintained)
- **Extensibility foundation**: Other modules inherit from `TimestampedDocument` rather than raw Beanie `Document`, allowing future cross-cutting concerns to be added at this layer
- **Single source of truth**: Changes to timestamp behavior (e.g., timezone handling, precision) happen in one place

**Role in system architecture:**
This module occupies the **data model foundation layer**. It sits between the Beanie ODM framework (external dependency) and all domain entity models (agent, comment, group, etc.). Every persistent entity in the system inherits from `TimestampedDocument`, making this the lowest-level architectural contract that all models must satisfy.

## Key Classes and Methods

### TimestampedDocument
**Purpose**: Base class for all MongoDB documents that require automatic timestamp management.

**Fields**:
- `createdAt: datetime` — The UTC timestamp when the document was first inserted into the database. Set once at creation time and never modified afterward.
- `updatedAt: datetime` — The UTC timestamp of the most recent modification (insert, replace, save, or update). Updated on every write operation.

Both fields default to `datetime.now(UTC)` at instantiation time, but are overridden by the event handlers before any database operation.

**Methods**:

1. `_set_created()` (decorated with `@before_event(Insert)`)
   - **When it runs**: Before any document is inserted for the first time
   - **What it does**: Sets both `createdAt` and `updatedAt` to the current UTC time at the moment of insertion
   - **Why both fields**: Ensures consistency; a newly created document has identical create and update timestamps initially
   - **Design note**: This overwrites any `createdAt` value set during object instantiation, ensuring the timestamp reflects actual database insertion time, not object creation time

2. `_set_updated()` (decorated with `@before_event(Replace, Save, Update)`)
   - **When it runs**: Before any document modification (full replacement, partial save, or atomic update)
   - **What it does**: Sets only `updatedAt` to the current UTC time
   - **Why only updatedAt**: Preserves `createdAt` unchanged; the original creation time must never shift
   - **Event scope**: Catches all three modification paths (Replace, Save, Update), covering Beanie's full mutation API

**Settings**:
- `use_state_management = True` — Enables Beanie's internal state tracking, allowing the library to detect which fields have changed and optimize update operations

## How It Works

**Document lifecycle and timestamp flow**:

1. **Instantiation**: A subclass (e.g., `Agent`) creates an instance of itself, inheriting from `TimestampedDocument`.
   ```
   agent = Agent(name="test")
   # At this point: agent.createdAt and agent.updatedAt are set to now(UTC) by Field defaults
   ```

2. **First database write (Insert)**: When the document is inserted for the first time via `.insert()` or `.save()`, Beanie triggers the `Insert` event.
   - `_set_created()` executes before the database operation
   - Both `createdAt` and `updatedAt` are reset to the exact moment of insertion
   - The document is written to MongoDB with both timestamps synchronized

3. **Subsequent modifications**: Any update operation (partial field change, full replace, or atomic update) triggers one of `Replace`, `Save`, or `Update` events.
   - `_set_updated()` executes, refreshing only `updatedAt`
   - `createdAt` remains unchanged (not touched by the event handler)
   - MongoDB receives the updated document with the new `updatedAt` but original `createdAt`

**Why three separate events for updates**:
- **Replace**: Full document replacement (all fields overwritten)
- **Save**: Partial save in Beanie (specific fields saved)
- **Update**: Direct MongoDB update operations (atomic changes)
Together, these cover all mutation pathways in Beanie, ensuring `updatedAt` is refreshed regardless of which API the caller uses.

**Edge cases and guarantees**:
- **UTC timezone**: Using `UTC` ensures timestamps are never ambiguous or dependent on server timezone
- **Monotonicity of createdAt**: Once set, `createdAt` never changes, providing an immutable audit anchor
- **updatedAt always progresses**: Each modification advances `updatedAt` (assuming time moves forward), enabling last-modified sorting and cache invalidation
- **No manual intervention**: Developers cannot override timestamps; the Beanie event system enforces this at the database layer

## Authorization and Security

This module does not implement authorization logic directly. However, it provides an important **audit trail foundation**:

- **Temporal accountability**: The `createdAt` and `updatedAt` fields enable systems to answer "when was this entity modified?" which is essential for compliance logging, debugging, and temporal queries
- **Assumption of trust**: This module assumes all callers have already been authorized by upstream layers (e.g., API routers with authentication). It does not validate who is modifying what; it only records when modifications occur.
- **Immutable creation record**: The unchangeable `createdAt` field provides forensic value; even if data is modified later, the original creation timestamp persists.

Downstream authorization systems (not in this module) should use these fields to enforce policies like "only admins can modify documents older than 30 days" or "creator can only delete within 1 hour."

## Dependencies and Integration

**Direct dependencies**:
- **Beanie** (`Document`, `Insert`, `Replace`, `Save`, `Update`, `before_event`): MongoDB async ODM framework. This module tightly couples to Beanie's event system to intercept and modify documents before database operations.
- **Pydantic** (`Field`): Data validation and serialization. Used to define field defaults with factory functions.
- **Python standard library** (`datetime`, `UTC`): Timezone-aware UTC timestamps.

**Dependent modules** (7 documented imports):
1. **agent** — User-facing agents (e.g., AI assistants) inherit from `TimestampedDocument` to track creation and modification times
2. **comment** — Comments on entities (posts, tasks, etc.) need temporal ordering; inherits for `createdAt` sorting
3. **group** — Workspace/organization groups track membership changes; timestamps enable audit logs
4. **message** — Chat or notification messages require `createdAt` for chronological ordering in conversations
5. **notification** — Notifications need `updatedAt` to determine staleness and read status age
6. **pocket** — A core entity (possibly a workspace subdivision) with temporal tracking requirements
7. **session** — User sessions track login/logout and activity; timestamps are critical for session expiration and audit
8. **workspace** — Top-level organizational entity; creation and modification timestamps are foundational for workspace lifecycle management

**Integration pattern**:
```python
# Example from workspace.py or similar:
from ee.cloud.models.base import TimestampedDocument

class Workspace(TimestampedDocument):
    name: str
    # ... other fields ...
    # Automatically gets createdAt and updatedAt tracking
```

Each dependent module adds its own domain-specific fields and methods while inheriting timestamp behavior automatically.

**System-wide implications**:
- All MongoDB queries can filter/sort by timestamp: `Workspace.find({"createdAt": {"$gte": start_date}})`
- API responses include temporal metadata for clients to track freshness
- Background jobs can identify stale entities (e.g., cleanup, archival)
- Audit systems have reliable temporal anchors for compliance reporting

## Design Decisions

**1. Automatic timestamp management via Beanie events**
- **Trade-off**: Developers cannot manually override timestamps (by design); code that attempts to set `createdAt` post-creation will fail silently because the event handler resets it.
- **Rationale**: Prevents accidental or malicious timestamp manipulation; the timestamp is a property of the system, not the data itself.
- **Alternative considered**: Manual timestamp management (developer sets fields). Rejected because it's error-prone and doesn't scale across 7+ models.

**2. UTC timezone exclusively**
- **Trade-off**: All timestamps are in UTC; display layers must handle timezone conversion for user-facing UI.
- **Rationale**: Eliminates ambiguity, simplifies comparisons, and aligns with international standards. A single source of truth for temporal ordering.

**3. Separate event handlers for Insert vs. Update**
- **Trade-off**: Code duplication (both set timestamps); conceptual distinction between creation and modification.
- **Rationale**: `createdAt` is immutable (set once at insertion); `updatedAt` is mutable (refreshed on every change). Separate handlers make this contract explicit and prevent future bugs if logic diverges.

**4. Field defaults via `Field(default_factory=...)`**
- **Trade-off**: Timestamps are set twice on insert (once by default_factory, then overridden by `_set_created`).
- **Rationale**: Ensures Pydantic validation passes (fields are never `None`), and the database always receives a second, more accurate timestamp. The tiny performance cost is negligible.

**5. use_state_management = True**
- **Trade-off**: Beanie tracks field changes in memory, adding memory overhead.
- **Rationale**: Enables partial updates and optimizes queries. Without this, every `.save()` would perform a full document replacement, defeating the purpose of selective updates.

**6. Inheritance-based composition**
- **Trade-off**: All entities must inherit from `TimestampedDocument` (tight coupling to this class).
- **Rationale**: Simpler than mixins or composition; leverages Python's class hierarchy cleanly. Mixin or decorator approaches would require more boilerplate for developers to get timestamps working.

## Architectural Principles

- **Separation of concerns**: Timestamp management is isolated from business logic (stored in subclasses)
- **DRY**: One implementation, many consumers
- **Immutable auditing**: Creation timestamp cannot change, ensuring forensic integrity
- **Eventual consistency ready**: Timestamps support distributed system concerns (causality, ordering)

---

## Related

- [agent-agent-configuration-and-metadata-storage-for-workspace-scoped-ai-agents](agent-agent-configuration-and-metadata-storage-for-workspace-scoped-ai-agents.md)
- [comment-threaded-comments-on-pockets-and-widgets-with-workspace-isolation](comment-threaded-comments-on-pockets-and-widgets-with-workspace-isolation.md)
- [group-multi-user-chat-channels-with-ai-agent-participants](group-multi-user-chat-channels-with-ai-agent-participants.md)
- [message-data-model-for-group-chat-messages-with-mentions-reactions-and-threading](message-data-model-for-group-chat-messages-with-mentions-reactions-and-threading.md)
- [notification-in-app-notification-data-model-and-persistence-for-user-workspace-e](notification-in-app-notification-data-model-and-persistence-for-user-workspace-e.md)
- [pocket-data-models-for-pocket-workspaces-with-widgets-teams-and-collaborative-ag](pocket-data-models-for-pocket-workspaces-with-widgets-teams-and-collaborative-ag.md)
- [session-cloud-tracked-chat-session-document-model-for-pocket-scoped-conversation](session-cloud-tracked-chat-session-document-model-for-pocket-scoped-conversation.md)
- [workspace-data-model-for-organization-workspaces-in-multi-tenant-enterprise-depl](workspace-data-model-for-organization-workspaces-in-multi-tenant-enterprise-depl.md)
