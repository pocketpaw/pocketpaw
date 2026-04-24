---
{
  "title": "PocketPaw Dashboard: FastAPI Web Server and API Gateway",
  "summary": "The dashboard module is PocketPaw's primary HTTP server, built on FastAPI with an asynccontextmanager lifespan. It mounts all API sub-routers, serves the frontend static files, manages channel adapter auto-start, and coordinates the WebSocket handler — acting as the integration point for every runtime subsystem.",
  "concepts": [
    "FastAPI",
    "dashboard",
    "lifespan management",
    "channel auto-start",
    "WebSocket authentication",
    "health heartbeat",
    "API routing",
    "APScheduler",
    "file browser",
    "mission control"
  ],
  "categories": [
    "Dashboard",
    "API Server"
  ],
  "source_docs": [
    "408d0cf95ba1fec0"
  ],
  "backlinks": null,
  "word_count": 462,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/dashboard.py` is the top-level FastAPI application for PocketPaw's web dashboard. As the entry point for all HTTP and WebSocket traffic, it wires together authentication, channel management, health monitoring, the proactive daemon, and the agent loop into a single server process.

## Application Structure

The FastAPI app is created with an `asynccontextmanager` lifespan that replaces the older `@app.on_event` pattern. Startup logic runs before `yield`; teardown runs after. This ensures clean shutdown even if the server is interrupted — channel adapters are stopped, schedulers are shut down, and rate limiter state is flushed.

## Routing Architecture

Dashboard routes are split across multiple modules to prevent the original `dashboard.py` from becoming a monolith. The main file mounts:

- `auth_router` from `dashboard_auth.py` — session tokens, cookies, QR codes
- `channels_router` from `dashboard_channels.py` — channel status, webhooks, extras
- `mount_v1_routers()` from `api/v1/` — the versioned REST API
- `deep_work_router` at `/api/deep-work/*` — Deep Work project orchestration
- Mission Control router at `/api/mission-control/*`

## Channel Auto-Start

During `startup_event()`, the dashboard iterates all configured channels and calls `_start_channel_adapter()` for each that is both configured and has auto-start enabled. Discord and Slack adapters connect via their respective bot SDKs; WhatsApp adapters set up webhook routes. This means users don't need to manually activate channels after restarting the server.

## Health Heartbeat

Added 2026-02-17: an APScheduler job runs `health_check()` every 5 minutes. If the health status changes between checks (e.g., a previously healthy check starts failing), a `health_update` WebSocket event is broadcast to all connected clients. This gives the dashboard UI real-time health status without polling.

## WebSocket Authentication Evolution

Before 2026-02-06, WebSocket auth was passed as a URL query parameter (`?token=...`), which is visible in server logs and browser history. The current implementation accepts auth via the first message sent after connection — the client sends `{"type": "auth", "token": "..."}` as the first frame. This keeps credentials out of URLs while still allowing the server to reject unauthorized connections before any sensitive data is exchanged.

## File Browse Fix

A notable bug was fixed (2026-02-12): `handle_file_browse()` was applying the 50-item limit before filtering hidden files (those starting with `.`). This meant a directory with 50+ hidden files would return an empty or near-empty listing. The fix reverses the order: filter hidden files first, then apply the limit.

## Dependency Guard

The entire module is wrapped in a `try/except ImportError` that catches missing `fastapi`, `uvicorn`, `qrcode`, and `jinja2`. If any are absent, a clear error message with install instructions is raised rather than a cryptic `ModuleNotFoundError` deep in the stack.

## Known Gaps

- The `startup_event()` function is long and handles many concerns. Partial startup failures (e.g., MCP server fails to start) log warnings but don't prevent the dashboard from running, which is intentional resilience but makes diagnosis harder.