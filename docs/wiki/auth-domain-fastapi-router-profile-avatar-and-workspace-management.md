---
{
  "title": "Auth Domain FastAPI Router — Profile, Avatar, and Workspace Management",
  "summary": "The auth router assembles PocketPaw Enterprise's full authentication HTTP surface by composing fastapi-users' built-in auth/register routes with custom profile management, avatar upload, and workspace-switching endpoints.",
  "concepts": [
    "fastapi-users",
    "auth router",
    "avatar upload",
    "path traversal prevention",
    "profile management",
    "workspace switching",
    "cookie transport",
    "bearer transport",
    "filesystem storage"
  ],
  "categories": [
    "auth",
    "API",
    "security"
  ],
  "source_docs": [
    "84518d015b5239df"
  ],
  "backlinks": null,
  "word_count": 366,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Router Composition

The router uses fastapi-users' router factories for the standard authentication flows:

```python
router.include_router(fastapi_users.get_auth_router(cookie_backend), prefix="/auth")
router.include_router(fastapi_users.get_auth_router(bearer_backend), prefix="/auth/bearer")
router.include_router(fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth")
```

This pattern delegates login/logout/register to fastapi-users' tested implementations while layering custom endpoints on top. The two auth routers under `/auth` and `/auth/bearer` give clients the choice of transport without duplicating the login logic.

## Profile Endpoints

- `GET /auth/me` — returns the full user profile via `AuthService.get_profile()`
- `PATCH /auth/me` — updates `full_name`, `avatar`, and `status` fields
- `POST /auth/set-active-workspace` — switches the user's active workspace context

These are thin router handlers that delegate to `AuthService`; no business logic lives in the router itself.

## Avatar Upload and Serving

Avatars are stored on the local filesystem under `~/.pocketpaw/avatars/`. The upload endpoint enforces:

- **Content type whitelist**: PNG, JPEG, WebP, GIF only — blocks script uploads masquerading as images
- **Size limit**: 5 MB hard cap with a `413` response
- **Old file cleanup**: before writing the new file, any existing avatar with a different extension is deleted to prevent accumulation of stale files

After writing, the user's `avatar` field is updated to a relative API path (`/api/v1/auth/avatar/{filename}`) rather than an absolute URL. This keeps the stored value valid regardless of how the server's hostname changes.

The serving endpoint (`GET /auth/avatar/{filename}`) includes a path traversal guard:

```python
if "/" in filename or "\\" in filename or ".." in filename:
    raise HTTPException(status_code=400, detail="Invalid filename")
```

This prevents a `?filename=../../etc/passwd` attack. Only filenames without directory separators or parent-directory references are served.

## Avatar Storage Architecture Note

The router comment notes that avatar storage uses the local filesystem "for now (could swap for S3/R2 later)". The path is constructed via `Path.home()` so it works on both macOS (Tauri desktop) and Linux (server deployments), but it means avatars are not replicated across multiple server instances.

## Known Gaps

- GIF uploads are accepted (in `_ALLOWED_AVATAR_TYPES`) but the frontend may not handle animated GIFs correctly in all avatar display contexts.
- Avatar serving has no cache-control headers, so browsers will re-fetch the image on every page load.
- Multi-instance deployments (load-balanced servers) will not share the filesystem avatar store — avatars uploaded to one instance are invisible from others.