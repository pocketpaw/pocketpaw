---
{
  "title": "Chat Service — Group and Message Business Logic",
  "summary": "The core business logic for the chat domain, split into GroupService (group CRUD, membership, agent management, DMs) and MessageService (send, edit, delete, reactions, threading, pins, search). Enforces authorization rules, emits events, and handles cursor-based pagination.",
  "concepts": [
    "GroupService",
    "MessageService",
    "cursor pagination",
    "soft delete",
    "toggle reaction",
    "event bus",
    "DM deduplication",
    "membership management",
    "agent management",
    "authorization guards",
    "slug generation",
    "N+1 queries"
  ],
  "categories": [
    "chat",
    "business logic",
    "services",
    "authorization"
  ],
  "source_docs": [
    "b44d60ea56388bb0"
  ],
  "backlinks": null,
  "word_count": 843,
  "compiled_at": "2026-04-08T07:26:37Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Chat Service — Group and Message Business Logic

`cloud/chat/service.py`

## Purpose

This is the largest and most critical module in the chat domain. It contains all business rules for groups and messages, separated from HTTP concerns (router) and data concerns (models). Both `GroupService` and `MessageService` are stateless classes with `@staticmethod` methods.

## Helper Functions

### Authorization Guards

- `_require_group_member(group, user_id)` — Raises `Forbidden` if user is not in `group.members`. Used to protect private/DM group access.
- `_require_group_admin(group, user_id)` — Raises `Forbidden` if user is not the group owner. Groups use a simple owner-is-admin model (no role-based permissions).

### Data Lookup

- `_get_group_or_404(group_id)` — Loads group or raises `NotFound`. Centralizes the null-check pattern.
- `_get_message_or_404(message_id)` — Loads a non-deleted message or raises `NotFound`. The `msg.deleted` check means soft-deleted messages are treated as nonexistent.

### Response Formatting

- `_group_response(group)` — Converts a `Group` document to a frontend dict. Notably, it **populates** member IDs and agent IDs by loading the corresponding `User` and `Agent` documents. This is an N+1 query pattern (one query per member) — acceptable for small groups but would need optimization for large ones. Failed lookups gracefully degrade to `{_id: uid, name: uid}`.
- `_message_response(msg)` — Flat dict conversion with camelCase keys for the frontend. Uses `model_dump()` on embedded documents (mentions, attachments, reactions).
- `_generate_slug(name)` — Produces URL-safe slugs: lowercase, hyphens for spaces, strip special chars. Double hyphens are collapsed.

## GroupService

### Group Lifecycle

- **create_group**: Creator is always added to the member list. DMs enforce exactly 2 members (sorted for consistent lookup). Public/private groups can have arbitrary initial members.
- **list_groups**: Returns public groups in the workspace PLUS private/DM groups where the user is a member. Uses a `$or` MongoDB query.
- **update_group**: Owner only. DMs cannot be updated (name, description are meaningless for DMs). Auto-regenerates slug when name changes.
- **archive_group**: Soft archive — sets `archived=True`. Archived groups reject new messages and member additions.

### Membership

- **join_group**: Public groups only. Idempotent — if already a member, the method succeeds without error.
- **leave_group**: Owners cannot leave (must transfer ownership first). This prevents orphaned groups.
- **add_members**: Owner only. Idempotent — skips already-present members. Only saves if at least one new member was added.
- **remove_member**: Owner only. Cannot remove the owner themselves.

### Agent Management

- **add_agent**: Prevents duplicate agents in a group with a validation error.
- **update_agent**: Only `respond_mode` is mutable.
- **remove_agent**: Filters agents list and raises `NotFound` if the agent wasn't present (length check).

### DM Management

- **get_or_create_dm**: Finds an existing DM between two users using `$all` + `$size` MongoDB operators on the sorted member list. Sorting ensures `[A, B]` and `[B, A]` produce the same query. Creates a new DM group if none exists.

## MessageService

### Core Operations

- **send_message**: Verifies membership, checks group is not archived, creates the `Message` document, updates group stats (`last_message_at`, `message_count`), and emits a `message.sent` event via the event bus. The event carries enough context for downstream handlers (notifications, agent triggers).
- **edit_message**: Author only. Sets `edited=True` and `edited_at` timestamp so the UI can show "(edited)".
- **delete_message**: Soft-delete (`deleted=True`). Both the author and the group owner can delete — this allows moderation by the group admin.

### Reactions

- **toggle_reaction**: Idempotent toggle behavior — if user already reacted with the emoji, remove their reaction; otherwise add it. Removes the entire `Reaction` entry when no users remain, keeping the reactions array clean.

### Pagination

- **get_messages**: Cursor-based pagination sorted newest-first. Cursor format is `"{iso_timestamp}|{object_id}"`. Uses `$or` with compound conditions to handle messages with identical timestamps (tiebreaker on `_id`). Fetches `limit + 1` to determine `has_more` without a separate count query.

### Threading

- **get_thread**: Finds all messages where `reply_to` equals the parent message ID, sorted ascending (oldest first). Verifies the user can access the parent message's group.

### Pins

- **pin_message** / **unpin_message**: Owner only. `pin_message` verifies the message belongs to the target group. Both operations are idempotent — pinning an already-pinned message does nothing; unpinning a non-pinned message raises `NotFound`.

### Search

- **search_messages**: Regex-based text search with `re.escape()` to prevent regex injection. Uses MongoDB's `$regex` with case-insensitive option. Limited to 50 results. Only searches non-deleted messages within a group the user can access.

## Design Decisions

- **N+1 queries in `_group_response`**: Each member/agent triggers a separate DB lookup. This is a conscious trade-off for simplicity over performance.
- **Owner-only admin model**: No per-member roles. Simple but limiting for larger teams.
- **Soft delete for messages**: Preserves message history and threading integrity.
- **Event bus emission**: `message.sent` event enables decoupled notification and agent response systems.

## Known Gaps

- N+1 query pattern in `_group_response` will degrade for groups with many members. Needs batch loading.
- No ownership transfer mechanism — if the owner's account is deleted, the group becomes unmanageable.
- `search_messages` uses MongoDB regex which doesn't leverage text indexes. For production-scale search, a dedicated search index (Atlas Search, Elasticsearch) would be needed.
- No pagination on `list_groups` — could return very large lists for workspaces with many groups.
- No rate limiting on `send_message`.
