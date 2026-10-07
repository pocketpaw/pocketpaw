# ee.cloud.__init__ — Cloud domain orchestration and FastAPI application bootstrap

> This module is the entry point for PocketPaw's enterprise cloud layer. It bootstraps a FastAPI application by mounting all domain routers (auth, workspace, agents, chat, pockets, sessions, knowledge base), registering a global error handler, configuring WebSocket endpoints, and initializing cross-domain event handlers and agent lifecycle management. It exists to centralize cloud infrastructure setup and enforce domain-driven architecture patterns across the system.

**Categories:** Cloud Domain — Orchestration, API Router — Bootstrap & Mounting, Infrastructure Layer — Lifecycle Management, Error Handling & Global Middleware, Event-Driven Architecture  
**Concepts:** mount_cloud(app), FastAPI application bootstrap, domain-driven architecture, router mounting, exception_handler decorator, CloudError, Depends() dependency injection, current_user, current_workspace_id, async/await patterns  
**Words:** 1588 | **Version:** 1

---

## Purpose

This module serves as the **orchestration and bootstrap layer** for PocketPaw's cloud domain architecture. Rather than requiring scattered application initialization code throughout the codebase, `mount_cloud(app)` is a single entry point that:

1. **Registers all domain routers** — Each domain (auth, workspace, agents, chat, pockets, sessions, knowledge base) has a thin `router.py` that declares HTTP endpoints. This function imports and mounts them all with a consistent `/api/v1` prefix.

2. **Installs a global error handler** — Catches `CloudError` exceptions from any domain and converts them to standardized JSON responses with appropriate HTTP status codes.

3. **Provides shared endpoints** — Some endpoints (user search, license info) don't belong to a single domain but serve cross-cutting concerns. They are defined here rather than duplicated.

4. **Configures infrastructure** — Registers event handlers for domain interactions, starts/stops the agent pool, and sets up WebSocket connections.

The module exists because **domain-driven design** requires separation of concerns: each domain (auth, chat, workspace) should be modular and self-contained, but the application still needs a single place to wire everything together. Without this module, the main application file would be cluttered with dozens of `include_router()` calls and scattered initialization logic.

## Key Classes and Methods

### Function: `mount_cloud(app: FastAPI) -> None`

**Purpose:** The primary entry point. Accepts a FastAPI application instance and mutates it by mounting all cloud infrastructure.

**How it works (in sequence):**

1. **Error Handler Registration** — Defines an async exception handler that catches any `CloudError` raised during request processing and returns a JSON response with the error's status code and serialized error data (via `exc.to_dict()`).

2. **Domain Router Mounting** — Imports routers from six domains:
   - `ee.cloud.auth.router` → handles authentication (login, signup, token refresh)
   - `ee.cloud.workspace.router` → workspace CRUD and settings
   - `ee.cloud.agents.router` → agent discovery and execution
   - `ee.cloud.chat.router` → message history and chat operations
   - `ee.cloud.pockets.router` → pocket (collection) management
   - `ee.cloud.sessions.router` → session tracking
   - `ee.cloud.kb.router` → knowledge base (documents, embeddings, search)
   
   Each router is mounted at `/api/v1`, so routes become `/api/v1/auth/login`, `/api/v1/workspace/...`, etc.

3. **User Search Endpoint** — Defines an inline `GET /api/v1/users` endpoint that:
   - Requires authentication via `current_user` dependency
   - Requires workspace context via `current_workspace_id` dependency
   - Takes optional `search` and `limit` query parameters
   - Queries the `UserModel` collection for users in the current workspace matching the search string (case-insensitive regex on email or full_name)
   - Returns a minimal user projection with `_id`, `email`, `name`, `avatar`, `status`
   
   **Why here?** This endpoint is used by group settings and pocket sharing features across multiple domains, so it's shared rather than duplicated.

4. **WebSocket Endpoint** — Registers the WebSocket handler from `ee.cloud.chat.router.websocket_endpoint` at `/ws/cloud` (no `/api/v1` prefix). This allows frontend clients to connect at `ws://host/ws/cloud?token=...` for real-time chat.

5. **License Endpoint** — Defines `GET /api/v1/license` (no authentication required) that returns license information via `get_license_info()`. Accessible to unauthenticated clients so they can check deployment license status.

6. **Event Handler and Agent Bridge Registration** — Calls:
   - `register_event_handlers()` — Sets up cross-domain event listeners (e.g., when a message is created, notify agents; when a pocket is shared, update permissions)
   - `register_agent_bridge()` — Initializes the agent execution bridge that allows chat endpoints to trigger agent workflows

7. **Agent Pool Lifecycle** — Registers FastAPI startup/shutdown handlers:
   - `@app.on_event("startup")` — Calls `get_agent_pool().start()` to initialize the agent pool when the app starts
   - `@app.on_event("shutdown")` — Calls `get_agent_pool().stop()` to gracefully shut down agents when the app stops

## How It Works

**Application Bootstrap Flow:**

```
Main application (e.g., main.py)
    ↓
    app = FastAPI()
    ↓
    mount_cloud(app)  ← This function
    ↓
    ├─ Install CloudError handler
    ├─ Import and mount 7 domain routers at /api/v1
    ├─ Add /api/v1/users search endpoint
    ├─ Add /ws/cloud WebSocket endpoint
    ├─ Add /api/v1/license endpoint
    ├─ Register event handlers and agent bridge
    └─ Register startup/shutdown hooks for agent pool
    ↓
    uvicorn.run(app)
```

**Request Handling with Error Recovery:**

When a client makes a request to any domain endpoint (e.g., `POST /api/v1/chat/messages`:

1. FastAPI routes it to the appropriate domain router
2. The router calls domain service logic (e.g., `ChatService.create_message()`)
3. If a `CloudError` is raised (e.g., `UnauthorizedError`, `NotFoundError`), FastAPI catches it via the exception handler registered in this module
4. The handler converts it to JSON with the appropriate status code
5. Client receives consistent error response

**WebSocket Connection Lifecycle:**

When a client connects to `ws://host/ws/cloud?token=...`:

1. FastAPI routes to `websocket_endpoint` from `ee.cloud.chat.router`
2. The endpoint validates the token (via dependency injection)
3. Connection is established for real-time chat
4. On shutdown, `_stop_agent_pool()` is called, which may gracefully disconnect all WebSocket clients

**User Search Flow:**

```
GET /api/v1/users?search=john&limit=10
    ↓
    current_user dependency → validates token, returns User object
    ↓
    current_workspace_id dependency → extracts workspace from token/context
    ↓
    Query UserModel with {workspaces.workspace: workspace_id, email/name matches search}
    ↓
    Return [{ _id, email, name, avatar, status }, ...]
```

## Authorization and Security

**Who can call what?**

| Endpoint | Authentication | Authorization | Notes |
|----------|---|---|---|
| `/api/v1/*` (domain routers) | Per-domain (auth router skips login route) | Per-domain (e.g., workspace membership, pocket ownership) | Each domain router applies its own checks |
| `/api/v1/users` | Required (`current_user`) | Required (`current_workspace_id`) | Can only search users in own workspace; useful for sharing/collaboration |
| `/ws/cloud` | Required (token in query param) | Required (workspace context) | Real-time chat; validates token before upgrading connection |
| `/api/v1/license` | **Not required** | None | Public endpoint; needed for license checks before login |

**Error Handling:**

The `CloudError` exception handler ensures that all domain errors are converted to standardized HTTP responses. The `CloudError` class (from `ee.cloud.shared.errors`) likely includes:
- `status_code` — HTTP status (401, 403, 404, 500, etc.)
- `to_dict()` method — Serializes error to JSON (message, error code, details)

This prevents information leakage and ensures consistent error contracts.

## Dependencies and Integration

**What this module imports (inbound dependencies):**

- **FastAPI** — Web framework for routing and dependency injection
- **ee.cloud.shared.errors.CloudError** — Base exception class for all cloud domain errors
- **ee.cloud.shared.deps** — `current_user`, `current_workspace_id` dependency functions
- **ee.cloud.shared.event_handlers.register_event_handlers** — Cross-domain event subscription setup
- **ee.cloud.shared.agent_bridge.register_agent_bridge** — Agent execution bridge
- **ee.cloud.auth.router** — Authentication domain (login, signup, token)
- **ee.cloud.workspace.router** — Workspace domain (CRUD, settings)
- **ee.cloud.agents.router** — Agent discovery and execution
- **ee.cloud.chat.router** — Chat domain (messages, WebSocket)
- **ee.cloud.pockets.router** — Pocket domain (collections, sharing)
- **ee.cloud.sessions.router** — Session domain (tracking, cleanup)
- **ee.cloud.kb.router** — Knowledge base domain (documents, search)
- **ee.cloud.license.get_license_info** — License information endpoint
- **ee.cloud.models.user.User** — User model for search endpoint
- **pocketpaw.agents.pool.get_agent_pool** — Agent pool lifecycle management

**What depends on this module:**

No other modules in the scanned set import from `ee.cloud.__init__`, but the main application (entry point) **must** call `mount_cloud(app)` after creating the FastAPI instance:

```python
# In main.py or similar
from fastapi import FastAPI
from ee.cloud import mount_cloud

app = FastAPI()
mount_cloud(app)  # ← Required to set up all cloud infrastructure
```

**Integration with other systems:**

- **Event System** — The `register_event_handlers()` call subscribes to events from each domain (message created, pocket shared, etc.) and triggers cross-domain actions
- **Agent System** — The `register_agent_bridge()` call allows chat endpoints to trigger agent execution; the startup/shutdown hooks ensure the agent pool is available during app lifetime
- **Authentication** — All endpoints rely on `current_user` and `current_workspace_id` dependencies, which are likely defined in `ee.cloud.shared.deps` and validate JWT tokens or similar
- **Database** — User search uses Beanie ODM (`UserModel.find()`, `.to_list()`) to query MongoDB

## Design Decisions

**1. Centralized Router Mounting (Facade Pattern)**

Instead of requiring the main application to import and mount 7+ routers independently, `mount_cloud()` acts as a facade. Benefits:
- Single point of change when adding/removing domains
- Main application stays clean and focused on infrastructure concerns
- Easier onboarding (developer only calls one function)

**2. Domain-Driven Architecture**

Each domain (auth, chat, workspace) is a separate module with:
- `router.py` — HTTP contract (thin, validation + routing)
- `service.py` — Business logic (stateless, testable)
- `schemas.py` — Pydantic models for validation

This module orchestrates these domains without enforcing strong coupling.

**3. Global Error Handler**

Rather than each route catching and converting `CloudError`, a single exception handler does it. Benefits:
- DRY principle — no repeated error handling code
- Consistent error responses across all domains
- Easy to add logging/monitoring to one place

**4. Inline Shared Endpoints**

The user search and license endpoints are defined inline here rather than in a separate "shared" domain. Rationale:
- They're small and cross-cutting
- Don't justify a full domain module
- Belong to infrastructure setup, not business logic

**5. WebSocket at Root Path**

The WebSocket is mounted at `/ws/cloud` (not `/api/v1/ws/cloud`) because:
- WebSocket clients often prefer a different path for routing/caching
- Avoids the `/api/v1` prefix convention (which is for REST APIs)
- Frontend knows to connect to `ws://host/ws/cloud`, not `wss://host/api/v1/...`

**6. Deferred Imports**

Domain routers are imported inside `mount_cloud()` rather than at module level. Benefits:
- Faster startup (don't load all domains if mounting is skipped)
- Circular import prevention (each domain can safely import shared utilities)
- Flexibility (could conditionally mount domains based on configuration)

**7. Agent Pool Lifecycle Management**

Using FastAPI's `on_event` hooks ensures the agent pool is:
- Started after all routers are mounted (so agents can access services)
- Stopped before the app exits (graceful shutdown)
- Integrated with the app's lifecycle (no separate background services to manage)

This is simpler than managing a separate thread or process.

**8. Public License Endpoint**

The `/api/v1/license` endpoint requires no authentication because:
- Clients need to know the license before authentication (deployment licensing)
- License info is non-sensitive (no user data, no private tokens)
- Simplifies client-side flow (no need to handle license checks in auth failures)

---

## Related

- [untitled](untitled.md)
- [workspace-data-model-for-organization-workspaces-in-multi-tenant-enterprise-depl](workspace-data-model-for-organization-workspaces-in-multi-tenant-enterprise-depl.md)
- [license-enterprise-license-validation-and-feature-gating-for-cloud-deployments](license-enterprise-license-validation-and-feature-gating-for-cloud-deployments.md)
- [deps-fastapi-dependency-injection-layer-for-cloud-router-authentication-and-auth](deps-fastapi-dependency-injection-layer-for-cloud-router-authentication-and-auth.md)
- [core-enterprise-jwt-authentication-with-cookie-and-bearer-transport-for-fastapi](core-enterprise-jwt-authentication-with-cookie-and-bearer-transport-for-fastapi.md)
- [agent-agent-configuration-and-metadata-storage-for-workspace-scoped-ai-agents](agent-agent-configuration-and-metadata-storage-for-workspace-scoped-ai-agents.md)
- [comment-threaded-comments-on-pockets-and-widgets-with-workspace-isolation](comment-threaded-comments-on-pockets-and-widgets-with-workspace-isolation.md)
- [file-cloud-storage-metadata-document-for-managing-file-references](file-cloud-storage-metadata-document-for-managing-file-references.md)
- [group-multi-user-chat-channels-with-ai-agent-participants](group-multi-user-chat-channels-with-ai-agent-participants.md)
- [invite-workspace-membership-invitation-document-model](invite-workspace-membership-invitation-document-model.md)
- [message-data-model-for-group-chat-messages-with-mentions-reactions-and-threading](message-data-model-for-group-chat-messages-with-mentions-reactions-and-threading.md)
- [notification-in-app-notification-data-model-and-persistence-for-user-workspace-e](notification-in-app-notification-data-model-and-persistence-for-user-workspace-e.md)
- [pocket-data-models-for-pocket-workspaces-with-widgets-teams-and-collaborative-ag](pocket-data-models-for-pocket-workspaces-with-widgets-teams-and-collaborative-ag.md)
- [session-cloud-tracked-chat-session-document-model-for-pocket-scoped-conversation](session-cloud-tracked-chat-session-document-model-for-pocket-scoped-conversation.md)
