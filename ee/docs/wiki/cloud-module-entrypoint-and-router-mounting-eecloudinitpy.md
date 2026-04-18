---
{
  "title": "Cloud Module Entrypoint and Router Mounting (ee/cloud/__init__.py)",
  "summary": "The central mounting function for PocketPaw Enterprise Cloud. `mount_cloud()` wires up all domain routers (auth, workspace, agents, chat, pockets, sessions), registers error handlers, event handlers, agent bridges, WebSocket endpoints, and manages the agent pool lifecycle.",
  "concepts": [
    "mount_cloud",
    "FastAPI router",
    "domain-driven design",
    "WebSocket",
    "agent pool",
    "error handler",
    "lifecycle events"
  ],
  "categories": [
    "architecture",
    "enterprise",
    "cloud",
    "API"
  ],
  "source_docs": [
    "4dadf1a410dd6875"
  ],
  "backlinks": null,
  "word_count": 448,
  "compiled_at": "2026-04-08T07:30:11Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Cloud Module Entrypoint and Router Mounting

## Purpose

`ee/cloud/__init__.py` is the single integration point between the FastAPI application and all enterprise cloud domains. The `mount_cloud(app)` function is called once during application startup to register everything the cloud tier needs.

## What `mount_cloud()` Does

### 1. Global Error Handler

Registers a `CloudError` exception handler that converts domain-specific errors into consistent JSON responses with appropriate HTTP status codes. This prevents internal exception details from leaking to clients.

### 2. Domain Router Mounting

Six domain routers are mounted under `/api/v1`:

| Domain | Responsibility |
|--------|---------------|
| `auth` | User registration, login, JWT management |
| `workspace` | Multi-tenant workspace CRUD |
| `agents` | Agent creation, configuration, knowledge base |
| `chat` | Real-time messaging, conversation history |
| `pockets` | Structured data containers |
| `sessions` | Agent conversation sessions |

### 3. Inline User Search Endpoint

A `/api/v1/users` GET endpoint is defined inline rather than in a separate domain. It searches users within the current workspace by email or name using case-insensitive regex matching. This exists here because it serves multiple domains (group settings, pocket sharing) and doesn't warrant its own domain module.

### 4. WebSocket Route

The WebSocket endpoint is mounted at `/ws/cloud` (root path, not under `/api/v1`) so the frontend can connect to `ws://host/ws/cloud?token=...`. This deliberate path choice avoids the versioned API prefix since WebSocket connections are long-lived and version-independent.

### 5. License Endpoint

An unauthenticated `/api/v1/license` endpoint returns license information. It's intentionally unprotected so the frontend can check license status before the user logs in.

### 6. Event Handlers and Agent Bridge

- `register_event_handlers()` — wires up cross-domain event listeners (e.g., when a chat message arrives, update the session timestamp)
- `register_agent_bridge()` — connects the cloud layer to the core agent runtime

### 7. Application Lifecycle

On startup:
- Registers chat persistence (saves WebSocket messages to MongoDB for durability)
- Starts the agent pool (pre-warms agent instances)

On shutdown:
- Stops the agent pool gracefully

## Architecture Notes

Imports are deliberately deferred (inside the function body) to avoid circular imports. The cloud domains reference each other and the core `pocketpaw` package — importing them at module level would create import cycles. By importing inside `mount_cloud()`, the full module graph is available.

## Known Gaps

- The inline user search endpoint uses `re.compile(re.escape(search))` for safety, but there's no pagination — only a `limit` parameter. Large workspaces could benefit from cursor-based pagination.
- `@app.on_event("startup")` and `@app.on_event("shutdown")` are deprecated in newer FastAPI versions in favor of lifespan context managers.
- The `cookie_secure=False` setting in the auth transport (visible in auth/core.py) is noted in a comment as needing to be `True` in production, but there's no environment-based toggle.