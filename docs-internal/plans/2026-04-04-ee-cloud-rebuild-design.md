# EE Cloud Module — Strip & Rebuild Design

**Date**: 2026-04-04
**Scope**: `ee/cloud/` only — strip and rebuild with clean architecture
**Consumer**: paw-enterprise (SvelteKit/Tauri desktop client)
**Runtime**: headless mode (`pocketpaw serve`), no dashboard dependency

## Context

The ee/cloud module (2400 LOC, 26 files) was built incrementally with hotfixes. It provides multi-tenant workspace, group chat, pockets, sessions, and agent management backed by MongoDB (Beanie ODM) and real-time via Socket.IO.

**Problems**: no service layer, no validation, global state, Socket.IO tightly coupled to ASGI, swallowed exceptions, circular imports, zero tests.

**Decision**: gut it, keep the Beanie models (cleaned up), rewrite all logic with domain-driven subpackages.

## Architecture: Domain Subpackages

```
ee/cloud/
├── auth/            # register, login, profile, JWT
│   ├── router.py
│   ├── service.py
│   └── schemas.py
├── workspace/       # workspaces, members, invites, SMTP
│   ├── router.py
│   ├── service.py
│   └── schemas.py
├── chat/            # groups, DMs, messages, reactions, threads, WebSocket
│   ├── router.py
│   ├── service.py
│   ├── schemas.py
│   └── ws.py
├── pockets/         # pockets, widgets, sharing via links, agents
│   ├── router.py
│   ├── service.py
│   └── schemas.py
├── sessions/        # session CRUD, runtime proxy, pocket auto-link
│   ├── router.py
│   ├── service.py
│   └── schemas.py
├── agents/          # agent discovery, CRUD
│   ├── router.py
│   ├── service.py
│   └── schemas.py
├── shared/          # cross-cutting concerns
│   ├── deps.py      # current_user, workspace_id, require_role()
│   ├── db.py        # MongoDB connection + Beanie init
│   ├── errors.py    # CloudError hierarchy + exception handler
│   ├── events.py    # internal async pub/sub for side effects
│   └── permissions.py  # role checks, pocket access, share link validation
├── models/          # existing Beanie models (cleaned up)
└── __init__.py      # mount all routers
```

## Data Model Changes

| Model | Changes |
|---|---|
| User | No change (fastapi-users BeanieBaseUser) |
| Workspace | Add `deleted_at` soft-delete, enforce seat limits at model level |
| Group | Add `last_message_at`, `message_count` counter |
| Message | Add `edited_at`, index on `(group_id, created_at)` for cursor pagination |
| Room | **Merge into Group** — DM is `type: "dm"` with 2 members |
| Pocket | Add `share_link_token`, `share_link_access` (view/comment/edit), `visibility` (private/workspace/public), `shared_with` (explicit user grants) |
| Session | Add `deleted_at` soft-delete |
| Invite | Add `revoked` flag, cleanup index on `expires_at` |
| Notification | Add `expires_at` for auto-cleanup |
| Comment, FileObj, Agent | No change |

**Session ↔ Pocket linking**: sessions auto-attach to pockets. Creating a pocket with `session_id` links the session. `Session.pocket_id` set on attachment.

## WebSocket Architecture (replacing Socket.IO)

Single endpoint: `ws://host/ws/cloud?token=<JWT>`

**Protocol** — typed JSON messages:

### Client → Server
- `message.send` — send message to group (content, reply_to)
- `message.edit` — edit own message
- `message.delete` — soft-delete message
- `message.react` — add/remove reaction
- `typing.start` / `typing.stop` — scoped to group, auto-expire 5s
- `presence.update` — online/away status
- `read.ack` — mark messages read up to ID

### Server → Client
- `message.new` — new message in group
- `message.edited` — message edited
- `message.deleted` — message deleted
- `message.reaction` — reaction added/removed
- `typing` — typing indicator
- `presence` — user online/offline/away
- `read.receipt` — read receipt
- `error` — error with code + message

**Design decisions**:
- Pydantic validation on every inbound message
- Group membership verified on every send
- Connection manager: `user_id → set[WebSocket]` (multi-tab/device)
- 30s grace period on disconnect before marking offline
- Graceful degradation: REST endpoints work without WebSocket

## Error Handling

```python
CloudError(status_code, code, message)
├── NotFound          # 404 — "group.not_found"
├── Forbidden         # 403 — "workspace.not_member"
├── ConflictError     # 409 — "workspace.slug_taken"
├── ValidationError   # 422 — "message.too_long"
└── SeatLimitError    # 402 — "workspace.seat_limit_reached"
```

Single exception handler returns: `{ "error": { "code": "...", "message": "..." } }`

## Internal Event Bus

| Event | Triggers |
|---|---|
| `invite.accepted` | notification + auto-add to default groups |
| `message.sent` | notifications for mentions, update group `last_message_at` |
| `pocket.shared` | notification for recipient |
| `member.removed` | cleanup group memberships, revoke pocket access |
| `session.created` | link to pocket if `pocket_id` provided |

Simple async callback registry, in-process.

## Permissions

- **Workspace roles**: owner > admin > member
- **Pocket access**: owner / edit / comment / view (explicit grants or share links)
- **Group access**: member check, public groups allow self-join
- **Share links**: token validated for expiry, revocation, access level
- **DMs**: any workspace member can DM any other member

## API Endpoints

### auth — `/api/v1/auth`
- POST `/register`, `/login`, `/logout`
- GET/PATCH `/me`
- POST `/password/reset`, `/password/reset/confirm`

### workspace — `/api/v1/workspaces`
- CRUD: POST/GET/PATCH/DELETE `/`, `/{id}`
- Members: GET/PATCH/DELETE `/{id}/members`, `/{id}/members/{uid}`
- Invites: POST `/{id}/invites`, GET/POST `/invites/{token}`, DELETE `/{id}/invites/{invite_id}`

### chat — `/api/v1/chat`
- Groups: POST/GET `/groups`, GET/PATCH `/{id}`, POST `/{id}/archive`, `/{id}/join`, `/{id}/leave`
- Members: POST/DELETE `/{id}/members`, `/{id}/members/{uid}`
- Agents: POST/PATCH/DELETE `/{id}/agents`, `/{id}/agents/{aid}`
- Messages: GET/POST `/{id}/messages`, PATCH/DELETE `/messages/{id}`
- Reactions: POST `/messages/{id}/react`
- Threads: GET `/messages/{id}/thread`
- Pins: POST/DELETE `/{id}/pin/{mid}`
- Search: GET `/{id}/search`
- DMs: POST `/dm/{user_id}`

### pockets — `/api/v1/pockets`
- CRUD: POST/GET/PATCH/DELETE `/`, `/{id}`
- Widgets: POST/PATCH/DELETE `/{id}/widgets`, `/{id}/widgets/{wid}`, POST `/{id}/widgets/reorder`
- Team: POST/DELETE `/{id}/team`, `/{id}/team/{uid}`
- Agents: POST/DELETE `/{id}/agents`, `/{id}/agents/{aid}`
- Sharing: POST/PATCH/DELETE `/{id}/share`, GET `/shared/{token}`
- Sessions: POST/GET `/{id}/sessions`

### sessions — `/api/v1/sessions`
- CRUD: POST/GET/PATCH/DELETE `/`, `/{id}`
- History: GET `/{id}/history`
- Touch: POST `/{id}/touch`

### agents — `/api/v1/agents`
- CRUD: POST/GET/PATCH/DELETE `/`, `/{id}`
- By slug: GET `/uname/{slug}`
- Discovery: POST `/discover`

### WebSocket — `/ws/cloud`
- JWT auth on connect, typed JSON protocol as described above
