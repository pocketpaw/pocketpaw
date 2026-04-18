---
{
  "title": "Enterprise Auth Core — JWT, User Management, and Admin Seeding (ee/cloud/auth/core.py)",
  "summary": "The authentication foundation for PocketPaw Enterprise Cloud. Implements JWT-based auth with dual transports (cookie for browsers, bearer for API/Tauri), user lifecycle management via fastapi-users, and idempotent admin seeding for first-run setup.",
  "concepts": [
    "JWT",
    "fastapi-users",
    "cookie auth",
    "bearer auth",
    "admin seeding",
    "idempotent",
    "UserManager",
    "dual transport"
  ],
  "categories": [
    "enterprise",
    "cloud",
    "authentication",
    "security"
  ],
  "source_docs": [
    "f9dca3381a04e3f4"
  ],
  "backlinks": null,
  "word_count": 469,
  "compiled_at": "2026-04-08T07:30:11Z",
  "compiled_with": "agent",
  "version": 1
}
---

# Enterprise Auth Core

## Purpose

`core.py` is the backbone of PocketPaw's enterprise authentication system. It configures the `fastapi-users` library with PocketPaw-specific settings: JWT strategy, dual transport backends, user management hooks, and admin seeding.

## Authentication Architecture

### Dual Transport Strategy

Two auth backends serve different client types:

| Backend | Transport | Use Case |
|---------|-----------|----------|
| `cookie` | `CookieTransport` (cookie name: `paw_auth`) | Browser-based dashboard |
| `bearer` | `BearerTransport` (token URL: `/api/v1/auth/login`) | API clients, Tauri desktop app |

Both use the same JWT strategy with shared secret and lifetime, so a token from one backend is valid in the other. The cookie transport uses `samesite=lax` and `secure=False` (development default).

### JWT Configuration

- **Secret**: from `AUTH_SECRET` env var, defaults to `"change-me-in-production-please"` — the default is intentionally obvious to flag insecure deployments
- **Lifetime**: 7 days (`60 * 60 * 24 * 7` seconds)
- Both password reset and verification tokens share the same secret

## User Manager

`UserManager` extends fastapi-users' base with lifecycle hooks:
- `on_after_register` — logs the new user's email and ID
- `on_after_login` — debug-level login logging

These hooks are minimal now but serve as the extension point for future features like welcome emails, analytics events, or workspace auto-provisioning.

## User Schemas

- `UserRead` — extends base with `full_name` and `avatar` fields for profile display
- `UserCreate` — extends base with `full_name` for registration

## Admin Seeding

`seed_admin()` is an **idempotent** function designed to run on every application startup:

1. Checks if admin email already exists in the database
2. If found, returns the existing user (no-op)
3. If not found, creates a superuser with verified status
4. Catches `UserAlreadyExists` as a race condition guard — if two startup processes run simultaneously, the second one gracefully handles the duplicate

**Default credentials** (from env vars with fallbacks):
- Email: `ADMIN_EMAIL` or `admin@pocketpaw.ai`
- Password: `ADMIN_PASSWORD` or `admin123`
- Name: `ADMIN_NAME` or `Admin`

The function logs the created admin's password in plaintext (`logger.info`). This is acceptable for local development but should be suppressed in production logging.

## Known Gaps

- **`SECRET` default is insecure**: `"change-me-in-production-please"` provides no security if the env var isn't set. A production deployment without `AUTH_SECRET` configured would have a guessable JWT secret.
- **`cookie_secure=False`**: The comment says "Set True in production with HTTPS" but there's no automatic toggle based on environment. This is a potential security risk if deployed to production without manual configuration.
- **Admin password logged in plaintext**: `logger.info("Admin user created: %s (password: %s)", email, password)` — this should be removed or gated behind a debug flag for production.
- **Shared secret for all token types**: password reset, verification, and auth tokens all use the same `SECRET`. Compromising one compromises all.
- **No rate limiting on auth endpoints**: brute-force attacks on login are possible without external rate limiting.