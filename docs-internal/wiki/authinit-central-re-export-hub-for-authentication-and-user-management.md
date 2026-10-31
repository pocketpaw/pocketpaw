# auth/__init__ — Central re-export hub for authentication and user management

> This module serves as the public API facade for the entire authentication domain, re-exporting core authentication utilities, user management classes, security backends, and routing. It exists to provide a clean, stable interface that shields downstream code from internal restructuring while maintaining backward compatibility. Within the system architecture, it acts as the single entry point for all auth-related functionality needed by other domains.

**Categories:** Authentication & Authorization, API Gateway Layer, Facade & Re-export Pattern, Security Infrastructure  
**Concepts:** FastAPI dependency injection, JWT (JSON Web Token) authentication, Beanie ODM, FastAPI-Users framework, cookie_backend, bearer_backend, UserManager, current_active_user, current_optional_user, Pydantic models (UserRead, UserCreate)  
**Words:** 1421 | **Version:** 1

---

## Purpose

This `__init__.py` module is a **re-export facade** that consolidates the authentication domain's public interface. Rather than forcing downstream modules to navigate the internal structure of the `ee.cloud.auth` package, this module collects everything important from two primary sub-modules (`core` and `router`) and exposes it under a single import namespace.

**Why it exists:**
- **Backward compatibility**: As the auth domain evolves internally, existing code importing from `ee.cloud.auth` continues to work without modification
- **Clear API boundary**: Explicitly defines what is "public" (re-exported) versus what is "private" (not exported). The `# noqa: F401` comments tell linters these imports are intentional despite appearing unused
- **Simplified imports**: Callers can write `from ee.cloud.auth import current_active_user` instead of `from ee.cloud.auth.core import current_active_user`
- **Single responsibility**: This file documents the contract of the auth domain at a glance

**Role in system architecture:**
The authentication domain is foundational—it manages user identity, credentials, session management, and authorization primitives. Every other domain (workspace, user, group, notification, etc.) depends on it to identify who is making requests and whether they have permission to proceed. This `__init__.py` ensures that critical abstractions like `current_active_user`, `fastapi_users`, and security backends are discoverable and stable.

## Key Classes and Methods

### Dependency Injection Helpers

**`current_active_user`**
- A FastAPI dependency that extracts the authenticated user from the current request context
- Used in route handlers as a function parameter; FastAPI automatically calls it and injects the result
- Raises an exception if no valid authentication token is present (enforces required auth)

**`current_optional_user`**
- A FastAPI dependency similar to `current_active_user`, but allows anonymous requests
- Returns `None` if no authentication token is present, otherwise returns the user object
- Useful for endpoints that support both authenticated and unauthenticated access

### User Management

**`UserManager`**
- The core service class responsible for user lifecycle operations: creation, retrieval, updates, password changes, verification
- Implements business logic for user validation, password hashing, and status transitions
- Likely uses Beanie ODM to persist users to the database

**`UserRead` and `UserCreate`**
- Pydantic models for serialization/deserialization
- `UserRead`: the shape of user data returned to clients (excludes passwords)
- `UserCreate`: the shape of data clients send when creating a new user (includes password)

**`seed_admin`**
- A utility function for initial system setup that creates the first admin user
- Called once during application bootstrap; prevents locking out of the system

### Security Infrastructure

**`fastapi_users`**
- A pre-configured FastAPI-Users instance that bridges the auth system to HTTP
- Provides standard routes like `/register`, `/login`, `/logout` and handles protocol details
- Integrates with the user database and security backends

**`get_jwt_strategy`, `get_user_manager`, `get_user_db`**
- FastAPI dependencies that provide access to core auth components
- `get_jwt_strategy`: returns the JWT token generation/validation logic
- `get_user_manager`: returns the UserManager instance for the current request
- `get_user_db`: returns the database accessor for users
- These are usually internal dependencies; rarely called directly by application code

**`cookie_backend` and `bearer_backend`**
- Two authentication backends supporting different credential formats
- `cookie_backend`: reads auth tokens from HTTP cookies (browser-friendly)
- `bearer_backend`: reads auth tokens from the `Authorization: Bearer <token>` header (API-friendly)
- Both backends produce valid sessions; a client can use either strategy

### Configuration

**`SECRET`**
- The cryptographic key used to sign and verify JWTs
- Must be kept confidential; compromise of `SECRET` allows forgery of any valid token
- Typically loaded from environment variables at startup

**`TOKEN_LIFETIME`**
- The duration (in seconds or timedelta) for which a JWT remains valid after issuance
- Represents the security vs. convenience trade-off: short lifetime requires frequent re-auth, long lifetime extends the window a stolen token is useful

### Routing

**`router`**
- A FastAPI `APIRouter` instance that mounts all authentication endpoints
- Typically includes login, logout, registration, password reset, and token refresh routes
- Imported directly and included in the main FastAPI app's routing configuration

## How It Works

**Import-time behavior:**
When `ee.cloud.auth` is first imported, this `__init__.py` executes, loading and re-exporting symbols from `core` and `router`. The `# noqa: F401` comments suppress linter warnings about unused imports—they are unused *locally* but used by *importers*.

**Typical authentication flow:**
1. A client makes an HTTP request with credentials (username/password) or a token (JWT in bearer header or cookie)
2. A route handler declares `current_user = current_active_user` as a dependency
3. FastAPI calls this dependency function, which validates the token/credentials against the auth backends
4. If valid, `current_user` is injected with the user object; the route handler executes with access to that user
5. If invalid, an HTTP 401/403 response is returned before the route handler runs

**Database integration:**
The `UserManager` and `get_user_db` work with a Beanie ODM backend (based on the import graph), persisting users to MongoDB. Password hashing is applied transparently—raw passwords are never stored.

**Token lifecycle:**
1. On login, `fastapi_users` creates a JWT signed with `SECRET` and sets `TOKEN_LIFETIME` as the expiration
2. The JWT is returned in the response (via cookie or body, depending on the backend)
3. Subsequent requests include this JWT
4. The `bearer_backend` or `cookie_backend` validates the JWT signature and expiration
5. If the token is expired, the client must re-authenticate (login again)

## Authorization and Security

**Authentication vs. Authorization:**
This module handles *authentication* (who are you?) but delegates *authorization* (what can you do?) to other domains. For example, workspace membership, role assignments, and resource permissions are likely determined by the `workspace`, `group`, and `permission` modules, which query this auth domain to learn the current user's identity.

**Token security:**
- Tokens are cryptographically signed with `SECRET`; forgery requires knowledge of the secret
- Tokens have a finite lifetime (`TOKEN_LIFETIME`); stolen tokens eventually expire
- Tokens should be transmitted over HTTPS to prevent interception
- The `cookie_backend` supports HttpOnly cookies, preventing JavaScript from accessing tokens (mitigates XSS token theft)
- The `bearer_backend` is stateless; the server doesn't maintain a session store, relying entirely on token signatures

**Access patterns:**
- `current_active_user` enforces authentication; endpoints using it require a valid token
- `current_optional_user` allows anonymous access; endpoints using it can serve both authenticated and unauthenticated clients
- Both return the User object, which other modules can then use to check permissions (e.g., does the user belong to this workspace?)

## Dependencies and Integration

**What this module depends on:**
- **`ee.cloud.auth.core`**: The concrete implementation of authentication logic, including `UserManager`, security backends, and JWT strategy
- **`ee.cloud.auth.router`**: FastAPI routes for login, registration, logout, etc.
- **External: FastAPI-Users library**: Provides the base `fastapi_users` instance and authentication patterns
- **External: Beanie ODM**: Likely used by `UserManager` to persist users to MongoDB
- **External: python-jose or similar**: JWT creation/validation

**What depends on this module:**
The import graph shows that other domains like `errors`, `workspace`, `license`, `user`, `group`, `invite`, `message`, `notification`, `pocket`, and `session` depend on auth. They import `current_active_user`, `current_optional_user`, or `UserManager` to:
- Inject the current user into route handlers
- Look up user metadata
- Validate that an action is performed by an authenticated principal
- Enforce workspace-scoped or role-based authorization

**Example integration:**
The `workspace` module might import `current_active_user` to ensure only authenticated users can create workspaces, then check workspace membership separately to enforce resource isolation.

## Design Decisions

**1. Facade pattern via re-exports**
Instead of keeping all exports in separate internal modules and requiring deep imports, this `__init__.py` collects them. Trade-off: slightly more code in `__init__.py`, but significantly improved external API clarity and refactor tolerance.

**2. Dual authentication backends (cookie + bearer)**
Supporting both cookies and bearer tokens allows the system to serve multiple client types (browsers, SPAs, native apps, server-to-server) from a single backend. Backends are plugged into `fastapi_users`; switching or adding backends requires only configuration, not code changes—good extensibility.

**3. Separation of concerns: core vs. router**
The `core` module encapsulates the business logic and data models; the `router` module adds HTTP semantics (request/response serialization, status codes, error messages). This separation makes the auth logic testable without HTTP, and allows multiple HTTP transports (REST, GraphQL, WebSocket) to reuse the same core logic if needed.

**4. Dependency injection for `UserManager`, JWT strategy, etc.**
Rather than exposing these as singletons or module-level variables, they are injected via FastAPI dependencies. This enables:
- Testing with mock implementations
- Per-request customization (e.g., different strategies for different clients)
- Lazy initialization and resource cleanup

**5. No explicit token revocation list**
Both backends are stateless—there is no server-side session store or revocation list. Once a token is issued, it's valid until expiration. This is appropriate for a distributed, scalable system but means logout cannot immediately invalidate tokens (the client must discard the token, and the server cannot force it). Some systems add a short-lived in-memory revocation cache for stronger logout guarantees.

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
