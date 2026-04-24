---
{
  "title": "Chat Domain Package Entrypoint",
  "summary": "The `ee/cloud/chat/__init__.py` file is the public entrypoint for the chat domain, re-exporting the APIRouter so the application can mount the entire chat surface with a single import. It acts as a stable boundary declaration hiding internal module layout.",
  "concepts": [
    "package entrypoint",
    "APIRouter",
    "re-export",
    "chat domain",
    "FastAPI routing",
    "module boundary",
    "domain package"
  ],
  "categories": [
    "chat",
    "cloud EE",
    "FastAPI",
    "architecture"
  ],
  "source_docs": [
    "6d9ca39e4ec93969"
  ],
  "backlinks": null,
  "word_count": 220,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `ee.cloud.chat` package organises the real-time chat domain: groups, messages, WebSocket connections, unread tracking, and schema definitions. The `__init__.py` is intentionally minimal, re-exporting only the `router` object from `ee.cloud.chat.router`.

## Why This Pattern?

FastAPI applications assemble their routing tree by including `APIRouter` instances into the main app. Without a clean package boundary, application startup code must know the internal layout of the chat domain. The `__init__.py` re-export hides this detail - consumers write `from ee.cloud.chat import router` and are insulated from internal restructuring.

## Domain Scope

The chat domain encompasses:

- **Groups** (`group_service.py`) - channels, DMs, agent DMs, membership management
- **Messages** (`message_service.py`) - CRUD, reactions, threads, pins, search
- **WebSocket** (`ws.py`) - real-time connection manager, presence, typing indicators
- **Unread tracking** (`unread_service.py`) - per-user unread and mention counters
- **Schemas** (`schemas.py`) - Pydantic models for requests, responses, WS envelopes
- **Router** (`router.py`) - REST endpoints and WebSocket handler
- **Service** (`service.py`) - backward-compatible re-export facade

## Relationship to `service.py`

A common pattern in this codebase is to keep a thin `service.py` re-export module so that pre-refactor imports (`from ee.cloud.chat.service import GroupService`) continue to work. `__init__.py` complements this by providing the router to application startup without exposing internals.

## Known Gaps

None for this file - its role is purely structural. Complexity lives in the sub-modules it aggregates.