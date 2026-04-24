---
{
  "title": "API Serve: Headless FastAPI Application for External Clients",
  "summary": "`create_api_app()` builds a standalone FastAPI application with `v1` REST routers, a `/ws` WebSocket endpoint, and full backend infrastructure (message bus, agent loop, scheduler) — but no dashboard frontend assets. This enables the `pocketpaw serve` command to power external clients like the Tauri desktop app or scripts without serving any HTML.",
  "concepts": [
    "create_api_app",
    "pocketpaw serve",
    "headless API mode",
    "FastAPI lifespan",
    "CORS middleware",
    "WebSocket",
    "mount_v1_routers",
    "dashboard_lifecycle",
    "Tauri desktop",
    "backend infrastructure"
  ],
  "categories": [
    "api",
    "FastAPI",
    "deployment",
    "external clients"
  ],
  "source_docs": [
    ""
  ],
  "backlinks": null,
  "word_count": 413,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`serve.py` is the entry point for PocketPaw's headless API mode. When users run `pocketpaw serve`, they get a fully functional AI agent backend accessible over HTTP and WebSocket, without the web dashboard's frontend assets. This is ideal for the Tauri desktop app (which provides its own Rust/HTML UI) and for production deployments where the frontend is served separately.

## Lifespan Management

Two `lifespan` context managers appear in the module: a module-level one and an inner one defined inside `create_api_app`. The inner one is the one actually used by FastAPI:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await lifecycle_startup()
    yield
    await lifecycle_shutdown()
```

`lifecycle_startup()` and `lifecycle_shutdown()` from `dashboard_lifecycle.py` initialize and tear down the full backend stack: the message bus, agent router, tool registry, scheduler, and any channel adapters. By reusing the same lifecycle functions as the dashboard mode, the API server guarantees identical behavior — a `pocketpaw serve` instance and a `pocketpaw dashboard` instance handle agent requests through the same code paths.

## Application Factory

`create_api_app()` returns a configured `FastAPI` instance with:

- **CORS middleware** — Allows the Tauri app (served from `tauri://` origin) and any configured origins to make cross-origin requests
- **Auth middleware** — The dashboard auth middleware stack applies to all routes, enforcing the same security model as the full dashboard
- **`mount_v1_routers(app)`** — Attaches all `/api/v1/` endpoints (chat, sessions, settings, channels, memory)
- **WebSocket endpoint at `/ws`** — The same real-time chat WebSocket available in the dashboard, using the same auth and message bus

## Separation from Dashboard Mode

The distinction from `pocketpaw dashboard` is purely about what is served over HTTP: the API server has no static file routes, no HTML templates, and no frontend JavaScript. The backend infrastructure is identical. This means:

- Bugs fixed in the agent loop affect both modes
- New features added to the v1 API are available in both modes
- Monitoring and health checks work the same way

## Known Gaps

- The module-level `lifespan` function (outside `create_api_app`) appears to be dead code — `create_api_app` defines its own inner `lifespan` and uses that. The outer one is never passed to a FastAPI app.
- CORS allowed origins are not shown in the source excerpt; if they default to `*`, the API server would accept cross-origin requests from any origin, which could be a security concern in network-accessible deployments.
- There is no documented way to run the API server and the dashboard simultaneously on different ports from the same PocketPaw process.
