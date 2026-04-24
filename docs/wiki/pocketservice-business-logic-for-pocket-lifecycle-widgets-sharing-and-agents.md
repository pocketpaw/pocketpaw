---
{
  "title": "PocketService: Business Logic for Pocket Lifecycle, Widgets, Sharing, and Agents",
  "summary": "PocketService is the stateless service class that encapsulates all pocket business logic — from CRUD and widget management through share link generation, collaborator access, and agent-generated pocket creation from ripple specs. Access control is enforced by two helper functions that distinguish owner-only actions from edit-access actions, and every mutation emits events to the realtime bus and shared event bus.",
  "concepts": [
    "PocketService",
    "stateless service",
    "access control",
    "ripple spec",
    "normalize_ripple_spec",
    "agent-generated pockets",
    "create_from_ripple_spec",
    "share links",
    "collaborators",
    "team members",
    "widget reorder",
    "Beanie ODM",
    "event bus",
    "audit logging"
  ],
  "categories": [
    "pockets",
    "service layer",
    "EE cloud",
    "access control"
  ],
  "source_docs": [
    "509a085a7b86c6be"
  ],
  "backlinks": null,
  "word_count": 588,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`ee/cloud/pockets/service.py` sits between the HTTP router and the MongoDB document model. It is a stateless class of static async methods, a pattern used throughout the EE cloud layer to keep the domain logic testable and importable without side effects.

## Access Control Helpers

Two module-level functions gate all mutations:

- `_check_owner(pocket, user_id)` — raises `Forbidden` if the caller is not the pocket owner. Used for share link operations and visibility changes.
- `_check_edit_access(pocket, user_id)` — raises `Forbidden` unless the caller is the owner, is in `shared_with`, or the pocket's visibility is `workspace`. This three-way check means workspace-visible pockets can be edited by any workspace member, which is the intended behaviour for shared project pockets.

Both helpers call `log_denial` from the audit module before raising, ensuring every rejected access attempt is recorded. The lazy import of `log_denial` inside the guard (`from pocketpaw.ee.guards.audit import log_denial`) prevents a circular import since the audit module itself may import from the shared layer.

## Create with Ripple Spec

The `create` method now accepts a full pocket spec upfront, building `Widget` objects from the request's `widgets` list and normalising `ripple_spec` via `normalize_ripple_spec` before persisting. A notable addition is the session link: if `session_id` is provided, the service fetches the matching `Session` document and sets `session.pocket` to point at the new pocket. This bridges the session and pocket domains without requiring a separate API call from the frontend.

## Agent-Generated Pockets

`create_from_ripple_spec` is a static method moved from `agent_bridge.py` to centralise pocket creation logic:

```python
@staticmethod
async def create_from_ripple_spec(
    workspace_id: str,
    owner_id: str,
    ripple_spec: dict,
    description: str = "",
) -> str | None:
```

It returns the new pocket's ID on success, or `None` on failure — never raising. This design is intentional: agent pipelines call this method as a best-effort operation. A failure to materialise a pocket should not abort the agent's response stream. The method extracts a name from multiple possible spec fields (`lifecycle.name`, `name`, `title`) before falling back to `"Agent-generated Pocket"`.

## Widget Operations

Widgets are stored as an embedded list on the `Pocket` document. The reorder method is the most defensively written: it builds a dict of all existing widgets by ID, consumes IDs from the requested order, then appends any remaining widgets that were not mentioned. This prevents silent data loss when the frontend sends a partial reorder list.

## Share Links

`generate_share_link` uses `secrets.token_urlsafe(32)` to produce 32 bytes of cryptographically random data, yielding a 43-character URL-safe token. The method is owner-only — calling `_check_owner` before writing ensures collaborators cannot generate or revoke share links.

## Collaborators vs Team

The service distinguishes two membership concepts:

- **Collaborators** (`shared_with`): Have edit access to the pocket content. Adding a collaborator also emits a `pocket.shared` event on the event bus.
- **Team members** (`team`): A looser association used for project organisation, accessible to any user with edit rights (not just the owner).

Both lists use idempotent append guards (`if member_id not in pocket.team`) to prevent duplicates, which prevents a double-call from a retry or race condition from inflating the list.

## Known Gaps

- **No soft-delete**: Pockets are hard-deleted via Beanie's `.delete()`. There is no `deleted_at` field or tombstone pattern.
- **No event emission on create/delete**: Unlike sessions, pocket creation and deletion do not emit realtime events. Only collaborator changes emit to the event bus.
- **Widget data validation**: Widgets are constructed from raw dicts without strict field validation, meaning a malformed widget dict from the frontend would surface as a runtime error during `.model_dump()` rather than a clean 400.