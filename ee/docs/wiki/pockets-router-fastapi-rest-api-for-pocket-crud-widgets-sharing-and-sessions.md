---
{
  "title": "Pockets Router: FastAPI REST API for Pocket CRUD, Widgets, Sharing, and Sessions",
  "summary": "FastAPI router defining the complete REST API surface for the Pockets domain. Covers pocket CRUD, widget management (add/update/remove/reorder), team and agent assignment, share link generation and access, collaborator management, and pocket-scoped session creation. All endpoints require a valid license and authenticated user context.",
  "concepts": [
    "FastAPI router",
    "PocketService",
    "dependency injection",
    "license gate",
    "CRUD",
    "widget management",
    "share link",
    "collaborator",
    "thin controller",
    "deferred import"
  ],
  "categories": [
    "Pockets",
    "API Layer",
    "REST API",
    "Routing"
  ],
  "source_docs": [
    "a9c86e797e5ef2f8"
  ],
  "backlinks": null,
  "word_count": 607,
  "compiled_at": "2026-04-08T07:26:05Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Pockets Router: FastAPI REST API for Pocket CRUD, Widgets, Sharing, and Sessions

## Purpose

This router is the HTTP interface for the Pockets domain. It translates HTTP requests into `PocketService` method calls, handling authentication and authorization via dependency injection. The router is a thin layer — all business logic lives in `PocketService`.

## Design Decisions

### License Gate
The router uses `dependencies=[Depends(require_license)]` at the router level, meaning ALL pocket endpoints require a valid enterprise license. This is a blanket gate — no pocket operations work without a license.

### Thin Controller Pattern
Every endpoint follows the same pattern: extract dependencies via `Depends()`, call the corresponding `PocketService` static method, return the result. There is zero business logic in the router. This makes the router trivially testable and keeps business rules centralized in the service layer.

### Dependency Injection for Auth Context
`current_user_id` and `current_workspace_id` are injected via FastAPI's `Depends()` system. This avoids passing raw request objects into the service layer and ensures authentication is enforced consistently.

### Deferred Session Import
The session endpoints use lazy imports:
```python
from ee.cloud.sessions.service import SessionService
```
This is inside the endpoint function body, not at module top level. This breaks a circular import: pockets depend on sessions (for creating pocket-scoped sessions) and sessions might depend on pockets. The deferred import resolves this cycle.

### Agent ID Flexibility
The `add_agent` endpoint accepts both `agentId` (camelCase) and `agent_id` (snake_case):
```python
agent_id = body.get("agentId") or body.get("agent_id")
```
This is a defensive pattern accommodating both frontend (camelCase) and backend (snake_case) callers. The `body` parameter is a raw `dict` rather than a Pydantic model, which is inconsistent with other endpoints — likely a shortcut that should be formalized.

## Endpoint Map

### Pocket CRUD
| Method | Path | Action |
|--------|------|--------|
| POST | `/pockets` | Create pocket |
| GET | `/pockets` | List user's pockets |
| GET | `/pockets/{pocket_id}` | Get single pocket |
| PATCH | `/pockets/{pocket_id}` | Update pocket |
| DELETE | `/pockets/{pocket_id}` | Delete pocket |

### Widgets
| Method | Path | Action |
|--------|------|--------|
| POST | `/pockets/{pocket_id}/widgets` | Add widget |
| PATCH | `/pockets/{pocket_id}/widgets/{widget_id}` | Update widget |
| DELETE | `/pockets/{pocket_id}/widgets/{widget_id}` | Remove widget |
| POST | `/pockets/{pocket_id}/widgets/reorder` | Reorder widgets |

### Team & Agents
| Method | Path | Action |
|--------|------|--------|
| POST | `/pockets/{pocket_id}/team` | Add team member |
| DELETE | `/pockets/{pocket_id}/team/{member_id}` | Remove team member |
| POST | `/pockets/{pocket_id}/agents` | Add agent |
| DELETE | `/pockets/{pocket_id}/agents/{agent_id}` | Remove agent |

### Sharing
| Method | Path | Action |
|--------|------|--------|
| POST | `/pockets/{pocket_id}/share` | Generate share link |
| DELETE | `/pockets/{pocket_id}/share` | Revoke share link |
| PATCH | `/pockets/{pocket_id}/share` | Update share link access |
| GET | `/pockets/shared/{token}` | Access via share link |

### Collaborators
| Method | Path | Action |
|--------|------|--------|
| POST | `/pockets/{pocket_id}/collaborators` | Add collaborator |
| DELETE | `/pockets/{pocket_id}/collaborators/{target_user_id}` | Remove collaborator |

### Sessions
| Method | Path | Action |
|--------|------|--------|
| POST | `/pockets/{pocket_id}/sessions` | Create pocket session |
| GET | `/pockets/{pocket_id}/sessions` | List pocket sessions |

## Known Gaps

- The `add_agent` and `add_team_member` endpoints accept raw `dict` bodies instead of typed Pydantic schemas. This bypasses request validation and is inconsistent with other endpoints.
- The `access_via_share_link` endpoint at `GET /pockets/shared/{token}` has no authentication — anyone with the token can access the pocket. This is by design for share links, but there is no rate limiting visible.
- No pagination on `list_pockets` or `list_pocket_sessions` — large workspaces with many pockets will return all results in a single response.
