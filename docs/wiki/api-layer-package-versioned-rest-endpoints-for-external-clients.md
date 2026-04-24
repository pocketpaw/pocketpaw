---
{
  "title": "API Layer Package: Versioned REST Endpoints for External Clients",
  "summary": "The `api` package provides a versioned REST API layer (`/api/v1/`) alongside backward-compatible aliases at `/api/`, designed for external clients such as the Tauri desktop application and automation scripts. It mounts alongside the dashboard routes rather than replacing them, enabling both UI and programmatic access to the same PocketPaw instance.",
  "concepts": [
    "REST API",
    "versioned endpoints",
    "external clients",
    "Tauri desktop",
    "FastAPI routers",
    "backward compatibility",
    "api/v1",
    "dashboard integration",
    "programmatic access",
    "API layer"
  ],
  "categories": [
    "api",
    "architecture",
    "external clients",
    "versioning"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 335,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `api` package was introduced in February 2026 to give external clients (the PocketPaw Tauri desktop app, shell scripts, third-party integrations) a stable, versioned HTTP surface. Before this existed, all HTTP access to PocketPaw went through dashboard routes that were tightly coupled to the frontend's needs and not designed for programmatic consumption.

## Design Goals

**Versioning**: Routes live under `/api/v1/`, establishing a versioning contract. External clients that pin to `v1` will not break when future versions add or change endpoints under `v2`. The backward-compat aliases at `/api/` (without version) bridge any clients that were built before versioning was introduced.

**External client focus**: The desktop app (Tauri) and scripts are the primary consumers. These clients provide their own UI and do not need the dashboard's HTML/JS assets. The API layer can be started in isolation via `pocketpaw serve` (handled by `serve.py` in this package), running a lightweight FastAPI app with only the API routers and WebSocket endpoint — no static file serving.

**Mounted alongside the dashboard**: For users who run the full PocketPaw dashboard, the API routes are mounted in the same FastAPI app. This means the dashboard's authentication middleware (cookie auth, session tokens, master token) applies to API routes as well, and the same message bus, agent loop, and scheduler backend serves both the dashboard UI and API clients.

## Package Contents

The package contains:

- `api_keys.py` — API key management (create, verify, revoke, rotate)
- `deps.py` — Shared FastAPI dependency (`require_scope`) for authorization
- `oauth2/` — OAuth2/PKCE authorization server for desktop app auth flows
- `serve.py` — Standalone FastAPI app factory for headless API-only deployments
- `v1/` — The versioned router collection (chat, sessions, settings, channels, memory endpoints)

## Known Gaps

- The backward-compat `/api/` aliases are not documented in the OpenAPI schema, making them invisible to clients using the auto-generated API docs.
- There is no rate limiting at the API layer — this is left to reverse proxy configuration (nginx, Caddy), which may not be present in all deployment scenarios.
