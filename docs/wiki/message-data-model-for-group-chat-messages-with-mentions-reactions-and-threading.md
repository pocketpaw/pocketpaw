# message — Data model for group chat messages with mentions, reactions, and threading support

> This module defines the Pydantic data models that represent chat messages in groups, including support for mentions, file attachments, emoji reactions, and message threading. It exists as a dedicated model layer to provide a single source of truth for message structure across the application, enabling consistent validation and serialization when messages are created, retrieved, or modified. The module serves as the bridge between the MongoDB persistence layer (via Beanie ODM) and higher-level services that need to work with message data.

**Categories:** Chat & Messaging, Data Model Layer, MongoDB/Beanie Persistence, Domain Model  
**Concepts:** Message, Mention, Attachment, Reaction, TimestampedDocument, group_id_indexing, compound_index, soft_delete, message_threading, user_mentions  
**Words:** 1718 | **Version:** 1

---

## Purpose

The `message` module defines the complete schema for group chat messages in PocketPaw. It exists to:

1. **Provide a single source of truth for message structure** — All code that reads or writes messages depends on these definitions, ensuring consistency across the codebase
2. **Enable validation at the boundary** — Pydantic models validate message data when it enters the system, catching malformed data before it reaches the database
3. **Support rich chat features** — The schema accommodates modern chat requirements: mentions (@user, @agent, @everyone), file/media attachments, emoji reactions, and threaded replies
4. **Enable MongoDB indexing for performance** — The `Message` class defines database indexes for the common query pattern of fetching messages from a group sorted by creation time

In the system architecture, this module sits in the **data model layer** — it defines the contract between the API layer (routers), the service layer (message_service), and the persistence layer (Beanie/MongoDB). Services and routers import and use these models when validating requests, transforming database documents, and returning responses to clients.

## Key Classes and Methods

### `Mention(BaseModel)`

Represents a mention (tag) of a user, agent, or group within message content.

**Fields:**
- `type: str` — The entity being mentioned: `"user"` (individual user), `"agent"` (bot/AI agent), or `"everyone"` (group mention)
- `id: str` — The unique identifier of the mentioned entity (User ID or Agent ID). Empty string for @everyone mentions
- `display_name: str` — The human-readable name shown in the UI (e.g., `"@rohit"`, `"@PocketPaw"`)

**Business logic:** When a user types `@rohit` in a message, the frontend or service layer creates a `Mention` object with `type="user"`, `id=<rohit_user_id>`, and `display_name="rohit"`. This structured format enables:
- Efficient querying of messages mentioning specific users
- Triggering notifications when a user is mentioned
- Rendering mentions with proper styling/links in the UI

### `Attachment(BaseModel)`

Represents a file, image, or other content attached to a message.

**Fields:**
- `type: str` — The kind of attachment: `"file"` (generic document), `"image"` (photo/screenshot), `"pocket"` (PocketPaw-specific content), or `"widget"` (embedded interactive component)
- `url: str` — The downloadable/viewable URL where the attachment can be accessed
- `name: str` — The display name of the attachment (e.g., filename or title)
- `meta: dict` — Flexible metadata store for attachment-specific data (e.g., image dimensions, file size, video duration)

**Business logic:** Supports flexible attachment handling. A `"file"` attachment might have `meta={"size_bytes": 1024000, "mime_type": "application/pdf"}`, while an `"image"` attachment might have `meta={"width": 1920, "height": 1080}`. The flexible `meta` field avoids schema changes when new attachment types or properties are added.

### `Reaction(BaseModel)`

Represents an emoji reaction (like a thumbs-up or heart) that users can add to a message.

**Fields:**
- `emoji: str` — The emoji character or code (e.g., `"👍"`, `"❤️"`, `":+1:"`)
- `users: list[str]` — List of User IDs who have reacted with this emoji to the message

**Business logic:** Multiple reactions can be stored in a message's `reactions` list. When User A adds a 👍 reaction that User B already added, the system appends User A's ID to the existing reaction's `users` list rather than creating a duplicate. This normalized structure enables efficient queries like "show me all messages I reacted to with 👍".

### `Message(TimestampedDocument)`

The core model representing a single chat message in a group, inheriting from `TimestampedDocument` which provides `createdAt` and `updatedAt` timestamps.

**Fields:**

**Routing & Identification:**
- `group: Indexed(str)` — The ID of the group this message belongs to. Indexed for fast queries like "fetch all messages in group X"
- `sender: str | None` — The User ID of who sent this message. `None` indicates a system message (e.g., "User X joined the group")
- `sender_type: str` — Whether the sender is a `"user"` (human) or `"agent"` (bot/AI). Allows distinguishing human conversations from system/bot messages
- `agent: str | None` — The Agent ID if `sender_type == "agent"`

**Content & Formatting:**
- `content: str` — The text body of the message
- `mentions: list[Mention]` — Users, agents, or groups mentioned in this message
- `attachments: list[Attachment]` — Files, images, or other content attached to this message

**Threading & Reactions:**
- `reply_to: str | None` — The message ID of the parent message if this is a reply (threaded conversation). `None` for top-level messages
- `reactions: list[Reaction]` — Emoji reactions users have added to this message

**Audit Trail:**
- `edited: bool` — Flag indicating whether this message has been edited after creation
- `edited_at: datetime | None` — Timestamp when the message was last edited. `None` if never edited
- `deleted: bool` — Soft delete flag. `True` means the message is logically deleted but remains in the database for audit/compliance

**Database Configuration (Settings class):**
```
name = "messages"  # MongoDB collection name
indexes = [[('group', 1), ('createdAt', -1)]]  # Compound index: group ascending, creation time descending
```

This index optimizes the most common query: "fetch messages from group X, sorted newest-first". The descending `createdAt` ensures fetching the latest messages without additional sorting overhead.

## How It Works

### Data Flow

1. **Inbound (API → Message creation):** A client sends a POST request with message data → the FastAPI router validates the request body as a `Message` object → Pydantic automatically validates types and constraints → the message_service receives the validated `Message` instance

2. **Persistence (Message → MongoDB):** The message_service calls Beanie ODM to save the `Message` → Beanie serializes the Pydantic model to JSON → MongoDB stores the document with the `createdAt`/`updatedAt` timestamps from `TimestampedDocument`

3. **Outbound (MongoDB → API response):** The service queries MongoDB and Beanie deserializes documents back to `Message` instances → the router serializes `Message` to JSON in the HTTP response → clients receive fully structured message objects

### Key Patterns

**Hierarchical composition:** `Message` contains lists of `Mention`, `Attachment`, and `Reaction` objects. Each is a small, focused model that can be used independently if needed, but gains meaning when embedded in a message.

**Optional fields for flexibility:** Fields like `sender` (null for system messages), `reply_to` (null for top-level messages), `agent` (null for human senders), and `edited_at` (null for unedited messages) allow one schema to represent multiple scenarios without requiring multiple models.

**Soft deletes:** `deleted: bool` flag allows messages to be "removed" from the UI while preserving the record for audit trails or compliance. Queries should filter `deleted == False` when fetching live messages.

**Metadata flexibility:** The `Attachment.meta` field uses a generic `dict` to avoid schema coupling. New attachment properties can be added without changing the model definition.

## Authorization and Security

This module itself does not enforce authorization — it is a pure data model. However, **authorization must be enforced at higher layers:**

- **Service layer (message_service):** Before a user can read messages from a group, the service must verify the user has permission to access that group
- **API router:** Request handlers should check that the authenticated user owns/can modify a message before allowing edits or deletes
- **Soft deletes:** The `deleted` flag is not access control; it's a UX feature. Deleted messages should still only be visible to users with audit/admin permissions

**Security considerations:**
- Message `content` is treated as user-generated text that may contain injection attacks; sanitization should occur in the service or router layer
- `Mention.id` and `sender` fields should be validated as real entity IDs before storage
- Attachment URLs should be validated for safe protocols (https, trusted domains) to prevent malicious links

## Dependencies and Integration

### Dependencies (Inbound)

- **`ee.cloud.models.base.TimestampedDocument`** — Base class providing `createdAt` and `updatedAt` fields. Used to track message creation and modification times
- **`beanie.Indexed`** — ODM utility for marking the `group` field as indexed in MongoDB
- **`pydantic`** — Provides `BaseModel` and `Field` for validation and schema definition

### Dependents (Outbound)

- **`message_service`** — The core service layer that creates, retrieves, updates, and deletes messages. Receives and returns `Message` instances
- **`router`** — FastAPI route handlers that expose message CRUD endpoints. Validates incoming requests as `Message` and serializes responses
- **`agent_bridge`** — Agent/bot integration that may create messages on behalf of agents. Uses `Message` model with `sender_type="agent"`
- **`service`** — Likely a facade or aggregator service that coordinates across multiple models
- **`__init__`** — Module exports `Message` and related classes for public use

### Example Integration Flow

```
User sends message via web client
  → FastAPI POST /groups/{groupId}/messages
    → router validates request body as Message
    → message_service.create_message(message)
      → Beanie ODM saves to MongoDB
      → Returns saved Message with generated IDs and timestamps
    → router returns JSON serialization of Message
  → WebSocket or polling updates other clients with new message
```

## Design Decisions

### 1. Compound Index on (group, createdAt)

The index is ordered `(group, 1), (createdAt, -1)` because the dominant query is "fetch all messages in a group, newest first". This avoids scanning all group messages or sorting in memory.

### 2. Mentions as Embedded List, Not Document Reference

Mentions are embedded as `list[Mention]` rather than as references to a separate `mention` collection. This keeps all message context in one document and avoids extra queries when retrieving a message. Trade-off: if mention display names change (e.g., user renames), old messages show stale names.

### 3. Soft Deletes (deleted: bool) Over Hard Deletes

Using `deleted: bool` instead of removing documents from the database provides:
- Audit trail (can see what was deleted and when)
- Thread continuity (replies to deleted messages remain readable)
- Regulatory compliance (some regulations require data retention)

Trade-off: queries must always filter `deleted == False`, and storage cost increases for deleted messages.

### 4. Flat Reactions List vs. Nested

Reactions are stored as `list[Reaction]` where each `Reaction` groups an emoji with the users who used it:
```json
{
  "emoji": "👍",
  "users": ["user_1", "user_2"]
}
```

Alternative (rejected): Store as `dict[emoji: list[user_id]]`. The chosen approach is more explicit and type-safe with Pydantic validation.

### 5. Optional sender for System Messages

Setting `sender: None` indicates a system message rather than creating a special `SystemMessage` subclass. This keeps the schema simpler and allows one query to fetch all messages in a group, both human and system.

### 6. Indexing Only group and createdAt

No index on `sender`, `reply_to`, or `edited` means queries like "find all messages sent by user X" or "find all edited messages" require full scans. This implies these queries are either rare, performed asynchronously (background jobs), or are not in the critical path. If user timelines or edit tracking become common queries, additional indexes should be added.

---

## Related

- [base-foundational-document-model-with-automatic-timestamp-management-for-mongodb](base-foundational-document-model-with-automatic-timestamp-management-for-mongodb.md)
- [untitled](untitled.md)
- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
