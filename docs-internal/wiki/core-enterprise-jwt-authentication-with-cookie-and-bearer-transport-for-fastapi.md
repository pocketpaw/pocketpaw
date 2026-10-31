# core — Enterprise JWT authentication with cookie and bearer transport for FastAPI

> This module implements a complete authentication system for PocketPaw using fastapi-users, providing user registration, login, logout, and profile management via both HTTP cookies (for browsers) and bearer tokens (for API/Tauri clients). It exists as a separate module to centralize all authentication concerns—user lifecycle, token strategies, session management—and to be imported by the router layer, which exposes these capabilities as REST endpoints. It forms the foundation of the enterprise auth architecture, sitting above the User data model and below the public API routers.

**Categories:** authentication, authorization, enterprise security, API layer, service layer  
**Concepts:** UserManager, UserRead, UserCreate, JWTStrategy, FastAPIUsers, BeanieUserDatabase, CookieTransport, BearerTransport, AuthenticationBackend, fastapi-users library  
**Words:** 1404 | **Version:** 1

---

## Purpose

This module solves the problem of **secure user authentication and session management** in a multi-client architecture (web browser + desktop Tauri app + API consumers). Rather than building authentication from scratch, it wraps `fastapi-users`—a battle-tested FastAPI authentication library—and configures it for PocketPaw's specific needs:

1. **Dual transport layer**: Browsers receive JWTs in HTTP-only cookies; API clients and Tauri app send JWTs in the `Authorization: Bearer` header. Both routes validate the same token.
2. **User lifecycle management**: Registration, email verification, password reset, and profile updates are handled by the `UserManager` class.
3. **Admin bootstrap**: The `seed_admin()` function ensures a default administrator account exists on first startup, reading defaults from environment variables.
4. **Enterprise-ready**: Supports superuser designation, verification tokens, and password reset workflows.

Within the system architecture, `core` is the **authentication engine**: it's imported by `router` (which wires endpoints) and depends on `user` (the User model), forming a clean separation between authentication mechanics and HTTP concerns.

## Key Classes and Methods

### `UserManager` — Lifecycle hooks and password handling

Inherits from `ObjectIDIDMixin` and `BaseUserManager[User, PydanticObjectId]`, extending fastapi-users' user manager:

- **`reset_password_token_secret`, `verification_token_secret`**: Shared secrets for generating secure tokens sent in password-reset and email-verification emails. Both use the `SECRET` constant.
- **`async on_after_register(user, request)`**: Hook called after a user signs up. Currently logs the registration event; could be extended to send welcome emails, create default workspace memberships, etc.
- **`async on_after_login(user, request, response)`**: Hook called after login. Logs the event; can be used for audit trails, analytics, or updating last-login timestamps.

### `UserRead` and `UserCreate` — Schemas

- **`UserRead`**: Pydantic model for serializing User responses. Extends `fastapi_users_schemas.BaseUser` and adds `full_name` and `avatar` fields for profile display.
- **`UserCreate`**: Pydantic model for registration payloads. Extends `fastapi_users_schemas.BaseUserCreate` and adds `full_name` for user-provided display names.

### `get_user_db()` — Database adapter (async generator)

Yields a `BeanieUserDatabase(User, OAuthAccount)` instance. This bridges fastapi-users' generic user store interface to the Beanie ODM layer. Each request gets its own instance via FastAPI dependency injection.

### `get_user_manager(user_db)` — Manager factory (async generator)

Creates a `UserManager` instance for each request, passing the user database. FastAPI will inject `user_db` by resolving the `get_user_db()` dependency. This pattern ensures each request has isolated, clean database and manager instances.

### `get_jwt_strategy()` — JWT token factory

Returns a `JWTStrategy` configured with:
- `secret`: The signing key (from `SECRET`)
- `lifetime_seconds`: Token expiration window (7 days)

Both cookie and bearer backends use the same strategy, ensuring tokens are interchangeable between transports.

### `seed_admin()` — Bootstrap admin account

**Purpose**: Ensure at least one superuser exists for initial system setup.

**Parameters** (all optional, fall back to env vars):
- `email`: Defaults to `ADMIN_EMAIL` env var or `admin@pocketpaw.ai`
- `password`: Defaults to `ADMIN_PASSWORD` env var or `admin123`
- `full_name`: Defaults to `ADMIN_NAME` env var or `Admin`

**Behavior**:
1. Checks if a user with `email` already exists; if so, returns it and logs.
2. Creates a new user via `UserManager.create()` with:
   - `is_superuser=True`: Grants admin privileges
   - `is_verified=True`: Skips email verification (admin doesn't need to verify their own email)
3. Re-saves the user to persist the `full_name` (note: `UserManager.create()` doesn't set custom fields).
4. Returns the created User or the existing one; returns None on unexpected errors.
5. Handles the `UserAlreadyExists` exception and re-queries the database (defensive pattern for race conditions).

## How It Works

### Authentication Flows

**Registration** (via router's `POST /auth/register`):
1. Client sends `{email, password, full_name}`.
2. FastAPI dependency injection calls `get_user_manager()` → `get_user_db()`.
3. `UserManager.create()` hashes the password, saves the User model to MongoDB, and calls `on_after_register()`.
4. Response includes `UserRead` serialization.

**Login** (via router's `POST /auth/login`):
1. Client sends `{username (email), password}`.
2. `UserManager` validates credentials (password hash comparison).
3. `JWTStrategy` generates a signed JWT token containing the user ID and claims.
4. **Cookie transport** sets `paw_auth` cookie with the token (HTTP-only, Lax SameSite).
5. **Bearer transport** returns token in response body for API clients.
6. `on_after_login()` is called for logging.

**Authorization** (on protected routes):
1. Browser: Cookie automatically sent; fastapi-users extracts `paw_auth` and validates.
2. API: `Authorization: Bearer <token>` header; fastapi-users extracts and validates.
3. Both extract the user ID from the JWT, re-fetch the User from MongoDB, and ensure `active=True`.
4. Request proceeds with the User available via `Depends(current_active_user)`.

### Token Lifetime and Expiration

`TOKEN_LIFETIME = 60 * 60 * 24 * 7` (7 days). Tokens expire after this window; clients must re-login. Refresh tokens are not implemented here (design choice: rely on login being lightweight with email/password or OAuth).

### Edge Cases

- **Token tampering**: JWT validation fails; request denied.
- **User deactivated after login**: Re-fetch on each request detects `active=False`; request denied.
- **Admin seeding race condition**: If two startup processes call `seed_admin()` simultaneously, the second catches `UserAlreadyExists` and re-queries. Beanie should handle database-level uniqueness constraints.
- **Missing SECRET env var**: Defaults to `"change-me-in-production-please"`, which is a loud warning but allows dev/test without setup.

## Authorization and Security

### Cookie Security

- **HTTP-only**: JavaScript cannot access `paw_auth`; mitigates XSS token theft.
- **Secure flag**: Set to `False` in code (comment says to enable in production with HTTPS). In production, this must be `True` to prevent transmission over unencrypted HTTP.
- **SameSite=Lax**: Mitigates CSRF attacks; cookie sent on safe cross-site requests (GET, navigation) but not on form POST or XHR from other origins.

### Bearer Token Security

- No built-in transport security; relies on HTTPS and request origin checks.
- Suitable for Tauri (native app, can't be phished easily) and trusted API consumers.

### JWT Secrets

- Both cookies and bearer tokens use the same `SECRET` for signing.
- If `SECRET` is leaked or rotated, all outstanding tokens become invalid immediately (no grace period).

### User Verification and Password Reset

- `reset_password_token_secret` and `verification_token_secret` are used by fastapi-users to generate secure time-bound tokens sent in emails.
- Not explicitly used in this file but configured; the router layer exposes the endpoints.

## Dependencies and Integration

### Imports from:

- **`ee.cloud.models.user`**: The `User` Beanie model, `OAuthAccount` (for OAuth2 integration, though not used here), and `WorkspaceMembership` (imported but not used in this file). These are the domain objects that represent authenticated users in the database.
- **`fastapi`, `fastapi_users`, `beanie`**: Third-party libraries providing the auth framework and database layer.

### Imported by:

- **`router`** (sibling module): Imports `fastapi_users`, `UserRead`, `UserCreate`, `current_active_user`, `current_optional_user`, and `seed_admin()` to define the actual REST endpoints.
- **`__init__`** (package init): May re-export key symbols for public API.

### How It Connects

This module is the **configuration layer** for authentication. It instantiates fastapi-users' machinery (managers, strategies, backends) without exposing endpoints. The router layer consumes these instances to build REST routes. The User model flows through the entire pipeline: created in `UserCreate`, persisted to MongoDB, retrieved in queries, and serialized in `UserRead`.

## Design Decisions

### Dual Transport Layer

**Why**: Single-page apps and desktop clients have different capabilities. Cookies require same-origin requests and CSRF protection; bearer tokens are RESTful and stateless but require client-side storage.

**Trade-off**: Dual transport adds complexity but allows the same backend to serve multiple client types seamlessly.

### Dependency Injection Pattern

`get_user_db()` and `get_user_manager()` are async generators that yield instances, relying on FastAPI's `Depends()` to manage their lifecycle. This ensures:
- Fresh database connections per request (isolation).
- Easy testing (inject mock managers).
- Clean separation of concerns (database creation vs. business logic).

### Hooks Over Middleware

Hooks like `on_after_register()` and `on_after_login()` are cleaner than post-request middleware for auth-specific side effects. They're called at the right moment in the user lifecycle and have access to the full context (user, request, response).

### Explicit Admin Seeding

`seed_admin()` is a function that must be **explicitly called** (e.g., in an app startup event), not automatic. This gives operators control: they can seed in a separate CLI command, in tests, or not at all in production (relying on OAuth or other flows).

### 7-Day Token Lifetime

**Why**: Long enough to avoid frequent re-logins (good UX for Tauri apps), short enough to limit the window of compromise if a token is stolen. No refresh tokens; users re-authenticate to get a new token (simple, secure, trades off UX slightly).

### Secrets in Environment Variables

Both `SECRET` and admin credentials come from env vars, enabling:
- Different secrets in dev, staging, production.
- Secrets not stored in code (reduced blast radius if repo is leaked).
- CI/CD pipeline integration (secrets injected at deploy time).

The fallback defaults are intentionally weak (`"change-me-in-production-please"`, `"admin123"`) to encourage setup without requiring manual tweaks for local dev, but loud enough to prompt security hardening before production.

---

## Related

- [untitled](untitled.md)
- [eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints](eecloudworkspace-router-re-export-for-fastapi-workspace-endpoints.md)
