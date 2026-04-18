---
{
  "title": "PocketService: Business Logic for Pocket CRUD, Widgets, Sharing, and Collaboration",
  "summary": "Stateless service class containing all business logic for the Pockets domain. Handles pocket CRUD, widget lifecycle (add/update/remove/reorder), share link generation and access, collaborator management, team and agent assignment, and session linking. Enforces ownership and edit-access authorization with dedicated guard functions.",
  "concepts": [
    "PocketService",
    "stateless service",
    "authorization",
    "access control",
    "widget CRUD",
    "share link",
    "token_urlsafe",
    "event bus",
    "pocket response serialization",
    "ripple normalization",
    "session linking"
  ],
  "categories": [
    "Pockets",
    "Business Logic",
    "Service Layer",
    "Authorization",
    "Collaboration"
  ],
  "source_docs": [
    "90c08f01f02190c8"
  ],
  "backlinks": null,
  "word_count": 829,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# PocketService: Business Logic for Pocket CRUD, Widgets, Sharing, and Collaboration

## Purpose

`PocketService` is the central business logic layer for the Pockets domain. It sits between the router (HTTP layer) and the data models (MongoDB layer), enforcing authorization rules, orchestrating multi-step operations, and emitting domain events. All methods are `@staticmethod` — the service is stateless, making it easy to test and reason about.

## Architecture

### Stateless Service Pattern
Every method is a `@staticmethod` that takes explicit parameters (pocket_id, user_id, body). There is no instance state, no constructor, no dependency injection container. This pattern:
- Makes each method independently testable
- Avoids hidden state that could cause test pollution
- Keeps the call signature honest about what data each operation needs

### Helper Functions
Three private helper functions are defined outside the class:

- **`_pocket_response(pocket)`**: Serializes a Pocket document to a frontend-compatible dict. This centralized serialization ensures consistent JSON shape across all endpoints. Notable: it uses `model_dump(by_alias=True)` for widgets to emit camelCase keys, and manually formats datetimes to ISO strings.

- **`_check_owner(pocket, user_id)`**: Raises `Forbidden` if the user is not the pocket owner. Used for destructive operations (delete, visibility change, share link management).

- **`_check_edit_access(pocket, user_id)`**: A more permissive check — allows owner, shared_with users, and workspace-visible pocket editors. Used for non-destructive mutations (add widget, update content).

- **`_get_pocket_or_404(pocket_id)`**: Fetches a pocket by ObjectId or raises `NotFound`. Centralizes the fetch-and-validate pattern to avoid repetitive null checks in every method.

## Key Operations

### Pocket Creation
`create()` handles a complex multi-step flow:
1. Build Widget objects from raw dicts (handling both camelCase and snake_case field names)
2. Normalize the ripple spec via `normalize_ripple_spec()` if provided
3. Insert the pocket document
4. If a `session_id` was provided, find and link the existing session to this pocket

The session linking step is important: when a user starts chatting and then decides to "save as pocket," the existing session should be associated with the new pocket rather than creating a new one.

### Access Control Model
The service implements a two-tier authorization model:

| Operation | Required Access |
|-----------|----------------|
| Delete pocket | Owner only |
| Change visibility | Owner only |
| Generate/revoke share link | Owner only |
| Add/remove collaborator | Owner only |
| Update pocket fields | Owner, shared_with, or workspace-visible |
| Add/update/remove widgets | Owner, shared_with, or workspace-visible |
| Add/remove team/agents | Owner, shared_with, or workspace-visible |
| View pocket | Owner, shared_with, workspace-visible, or share link |

### Widget Operations
Widget CRUD operates on the embedded widget array:
- **Add**: Creates a new `Widget` with an auto-generated ObjectId, appends to the array, saves the parent pocket.
- **Update**: Finds widget by ID using `next()` iterator, applies non-None fields, saves parent.
- **Remove**: Filters the widget array excluding the target ID. Compares array length before/after to detect "widget not found" (raises `NotFound` if lengths match).
- **Reorder**: Builds an ID-to-widget map, reorders by the provided ID list, appends any unlisted widgets at the end. This is graceful — partial reorder lists do not lose widgets.

### Share Link System
Share links use `secrets.token_urlsafe(32)` for cryptographically secure tokens. The flow:
1. **Generate**: Creates token, stores on pocket, returns URL path
2. **Access**: Finds pocket by token (no auth required — the token IS the auth)
3. **Update**: Changes access level while preserving existing token
4. **Revoke**: Clears token and resets access to "view"

The revoke operation resets `share_link_access` to "view" as a defensive default — if a new share link is generated later, it starts with minimal permissions.

### Event Bus Integration
The `add_collaborator` method emits a `pocket.shared` event after adding the collaborator:
```python
await event_bus.emit("pocket.shared", {...})
```
This enables downstream side effects (notifications, activity feeds, analytics) without coupling the pocket service to those concerns.

## Known Gaps

- **No pagination**: `list_pockets()` returns all matching pockets via `.to_list()`. For workspaces with hundreds of pockets, this will be slow and memory-intensive.
- **No transactions**: Multi-step operations (create pocket + link session) are not wrapped in MongoDB transactions. If the session link fails after pocket creation, the pocket exists without its intended session link.
- **Hard delete**: `delete()` uses `await pocket.delete()` (hard delete) while other entities in the codebase use soft delete. This means deleted pockets cannot be recovered and any references to them (from sessions, messages, etc.) become dangling.
- **Widget ID validation**: The raw dict widget creation in `create()` does not validate widget data against the Widget model's constraints — invalid widget data could slip through.
- **No workspace scoping on get/update/delete**: Individual pocket operations only check user access, not workspace membership. A user with access to a pocket in workspace A could potentially access it from workspace B's API context.
- **Collaborator access level stored but not enforced**: `AddCollaboratorRequest` accepts an `access` level (view/comment/edit) and emits it in the event, but `_check_edit_access()` only checks if the user ID is in `shared_with` — it does not check what access level they were granted.
