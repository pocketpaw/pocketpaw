# group — Multi-user chat channels with AI agent participants

> This module defines the data models for chat groups/channels that support multiple human users and AI agent participants, similar to Slack channels. It exists as a separate concern to cleanly separate the group entity definition from business logic, enabling other modules (group_service, routers, event handlers) to depend on a single source of truth for group structure. As a foundational data model, it sits at the core of the chat/collaboration system architecture.

**Categories:** data model — core persistent entity, chat/collaboration — domain area for group conversations, multi-user feature — supports multiple participants with different roles, MongoDB/Beanie — database technology and ORM layer  
**Concepts:** Group — multi-user conversation space entity, GroupAgent — agent assignment with configurable response behavior, TimestampedDocument — base class adding created_at/updated_at, Workspace scoping — tenant isolation via workspace field, Soft delete pattern — archived flag instead of hard delete, Denormalization — message_count, last_message_at, pinned_messages cached on group, Composite indexing — (workspace, slug) index for efficient tenant-scoped queries, Respond mode — agent participation control (mention_only, auto, silent, smart), Type validation — pattern regex for public/private/dm type field, Beanie ODM — MongoDB object mapping and indexing  
**Words:** 1325 | **Version:** 1

---

## Purpose

The `group` module defines the persistent data structures for multi-user conversation spaces within a workspace. It solves the architectural problem of representing "channels" or "groups" where:

- Multiple **human users** can participate together
- **AI agents** can be assigned with different participation modes (mention-only, auto-respond, silent, or smart modes)
- Groups can have different visibility levels (public, private, direct message)
- Metadata like messages, pins, and activity tracking are maintained

This module exists separately because the Group entity is referenced by many other parts of the system (group_service for business logic, routers for HTTP endpoints, event_handlers for real-time updates, agent_bridge for agent interactions). By centralizing the data model, the system maintains a single source of truth about what a group is, avoiding duplication and drift.

## Key Classes and Methods

### GroupAgent

Represents a single AI agent assignment within a group with configurable behavior:

**Fields:**
- `agent: str` — The unique identifier of the AI agent being assigned
- `role: str` — The agent's responsibility level: `"assistant"` (responds helpfully), `"listener"` (observes only), or `"moderator"` (enforces rules). Defaults to `"assistant"`.
- `respond_mode: str` — Controls when the agent participates:
  - `"mention_only"` — Only responds when explicitly mentioned (default, lowest noise)
  - `"auto"` — Responds to all messages automatically (highest engagement)
  - `"silent"` — Never responds, purely observational
  - `"smart"` — Responds intelligently based on context and relevance

**Business Logic:** This is a composition pattern allowing flexible agent configuration without modifying group structure itself. An agent can have both a role (what it does) and a respond mode (when it does it).

### Group

The core persistent entity representing a conversation space, extending `TimestampedDocument` (which adds `created_at` and `updated_at`).

**Core Fields:**
- `workspace: Indexed(str)` — Workspace ID; indexed because queries almost always filter by workspace (tenant isolation)
- `name: str` — Human-readable group name (e.g., "engineering-chat")
- `slug: str` — URL-safe identifier (derived from name, enables `/groups/{slug}` URLs)
- `description: str` — Optional group purpose/topic
- `icon: str`, `color: str` — UI presentation metadata
- `type: str` — Visibility/access control: `"public"` (all workspace members), `"private"` (invite-only), `"dm"` (direct message between 2-3 people). Validated with regex pattern.

**Participants:**
- `members: list[str]` — User IDs of human participants (defaults to empty; populated when users join)
- `agents: list[GroupAgent]` — AI agents assigned to this group with their individual configs
- `owner: str` — User ID of the group creator/owner (used for permission checks)

**Content and Activity:**
- `pinned_messages: list[str]` — Message IDs of messages pinned to top (denormalized for quick retrieval)
- `message_count: int` — Running counter of total messages (for analytics, pagination hints)
- `last_message_at: datetime | None` — Most recent message timestamp (enables "updated recently" sorting and activity detection)

**Lifecycle:**
- `archived: bool` — Soft delete: `True` means the group is inactive but preserved for history (allows unarchiving)

**Database Settings:**
- `Settings.name = "groups"` — MongoDB collection name
- `Settings.indexes` — Composite index on `(workspace, slug)` ensures slug uniqueness within a workspace and enables fast lookups by workspace + slug

## How It Works

### Data Flow

1. **Group Creation:** When a user creates a group via the router, a Group instance is instantiated with `owner=user_id`, `members=[owner]` initially, `workspace=current_workspace`, and timestamp defaults.
2. **Agent Assignment:** The group_service receives a list of GroupAgent configs and appends them to the `agents` field. Each GroupAgent specifies an agent ID and its participation rules.
3. **Message Ingestion:** When messages arrive (from event_handlers or message_service), the `message_count` is incremented and `last_message_at` is updated.
4. **Querying:** The router typically queries groups by `workspace + slug` (leveraging the composite index) to fetch a specific group, or by `workspace + archived=False` to list active groups.
5. **Agent Bridge Integration:** The agent_bridge reads the `agents` list and `respond_mode` to determine when/how to invoke each agent on new messages.

### Edge Cases and Design Decisions

**Soft Delete Pattern:** The `archived` field is a soft delete—groups are never truly removed, preserving message history and audit trails. This is critical for compliance and customer support ("when was this discussion?" can always be answered).

**Denormalized Message Metadata:** Fields like `message_count`, `last_message_at`, and `pinned_messages` are denormalized (stored on the group document rather than computed from a messages collection). This trades write complexity for read speed—displaying a group list requires zero joins. The group_service is responsible for keeping these consistent when messages are created/deleted.

**Workspace Scoping:** Every group belongs to exactly one workspace, enforced at the data model level via the `workspace` field. This is foundational multi-tenancy: queries always filter by workspace, preventing accidental cross-tenant leaks.

**Type Validation:** The `type` field uses Pydantic's `pattern` validator to ensure only valid types are accepted, failing fast at deserialization rather than allowing invalid states.

**Optional Timestamps:** `last_message_at` is `None` for newly created groups with no messages, allowing the system to distinguish "no messages yet" from "very old last message."

## Authorization and Security

This module itself has no authorization logic—it's a pure data model. However, it provides the structure that enables authorization elsewhere:

- **Ownership Check:** Routers and services use the `owner` field to verify if the requesting user can delete/edit group settings.
- **Membership Check:** The `members` list determines if a user can view/post messages in the group.
- **Type-Based Access:** The `type` field signals to upstream logic whether access is public (no check), invite-only (check membership), or DM (check if one of exactly 2 members).

The actual enforcement happens in group_service and routers, not here.

## Dependencies and Integration

### Dependencies (What This Module Imports)

- **`base` module:** Imports `TimestampedDocument`, a base class that adds `created_at` and `updated_at` fields. This is a foundational abstraction for all persistent entities in the system.
- **`beanie`:** ODM (Object-Document Mapper) providing `Indexed()` for marking fields for database indexing. Beanie handles the mapping between Python objects and MongoDB documents.
- **`pydantic`:** Type validation and serialization. `BaseModel` and `Field` enable runtime type checking, JSON schema generation, and error messages.
- **`datetime`:** Standard library for timestamp types.

### Reverse Dependencies (What Imports This Module)

- **`group_service`:** Contains business logic for creating, updating, querying, and archiving groups. Reads and modifies Group instances.
- **`router`:** HTTP API endpoints for group CRUD operations. Serializes/deserializes Group instances to/from JSON.
- **`__init__` (package init):** Re-exports Group and GroupAgent for public API (other modules import from the models package).
- **`agent_bridge`:** Reads the `agents` list and `respond_mode` to dispatch messages to appropriate agents.
- **`event_handlers`:** Listens for group events (creation, member join, message arrival) and updates Group fields or triggers side effects.

### Integration Points

```
Group (this module)
  ↓ extends
TimestampedDocument (base module)

Used by:
  ├─ group_service: CRUD operations, membership management
  ├─ router: HTTP API endpoints
  ├─ agent_bridge: Agent dispatch logic
  ├─ event_handlers: Event processing and state updates
  └─ __init__: Public API exports
```

## Design Decisions

**Composition over Inheritance for Agents:** Rather than creating a GroupWithAgents subclass, GroupAgent is a simple Pydantic model nested in the agents list. This keeps the design flat and allows agents to be added/removed without restructuring the group document.

**Beanie ODM + Pydantic:** Using Beanie (MongoDB ODM) with Pydantic models provides automatic validation, JSON serialization, and database mapping. This reduces boilerplate but ties the system to MongoDB; switching databases would require replacing Beanie.

**Indexed Workspace Field:** The `workspace` field is indexed individually because it's a frequent filter dimension ("show me all groups in my workspace"). The composite `(workspace, slug)` index is more specific and handles slug lookups efficiently.

**Denormalization Over Normalization:** Storing `message_count` and `last_message_at` on the group avoids expensive aggregations when listing groups. The trade-off is that group_service must keep these consistent, accepting higher write latency for lower read latency.

**Soft Delete with No Purge:** Archived groups are never deleted, supporting compliance, audit trails, and unarchive scenarios. A purge operation would require explicit administrative action and would not be automatic.

**Flexible Agent Modes:** The `respond_mode` field is a string enum (not a Python Enum class) for simplicity and JSON compatibility. The system is extensible: new modes can be added without code changes, only service logic updates.

---

## Related

- [base-foundational-document-model-with-automatic-timestamp-management-for-mongodb](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md)
- [untitled](untitled.md)
- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
