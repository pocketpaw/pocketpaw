---
{
  "title": "Notifications Domain Package",
  "summary": "The notifications `__init__.py` serves as the package boundary marker for PocketPaw's in-app notifications domain, grouping the service, router, and response schemas into a cohesive module. No symbols are re-exported; the package is imported by the application factory to register the router.",
  "concepts": [
    "domain package",
    "vertical-slice architecture",
    "FastAPI router",
    "NotificationService",
    "package organization",
    "bounded domain",
    "module docstring"
  ],
  "categories": [
    "notifications",
    "architecture",
    "enterprise-cloud"
  ],
  "source_docs": [
    "9fe731c5e2cdf4a9"
  ],
  "backlinks": null,
  "word_count": 353,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `ee/cloud/notifications/` package is a bounded domain in PocketPaw's enterprise cloud tier that owns all in-app notification logic: creating notifications, listing them per user, marking them read, bulk-clearing them, and pushing realtime events to connected clients.

## Package Organization

The domain is split into three focused modules:

- **`models/notification.py`** — MongoDB document schema (`Notification`, `NotificationSource`).
- **`service.py`** — Stateless CRUD class (`NotificationService`) that writes/reads the document and emits realtime events.
- **`router.py`** — FastAPI `APIRouter` that exposes REST endpoints (`GET /notifications`, `POST /notifications/{id}/read`, `POST /notifications/clear`).
- **`schemas.py`** — Pydantic response schema (`NotificationResponse`) for typed API responses.

## Why a Separate Domain Package?

PocketPaw's EE cloud is organized into domain packages under `ee/cloud/`. Each domain package owns its own router, service, and schema, following a vertical-slice architecture. This keeps notification logic isolated from the messaging, pocket, and user domains — a change to how notifications are delivered does not require touching message or pocket code.

## The `__init__.py` Role

The file contains only a module docstring: `"Notifications domain — service, router, and schemas for in-app notifications."` This serves two purposes:

1. **Package declaration** — Python requires `__init__.py` to treat the directory as a package, enabling `from ee.cloud.notifications.service import NotificationService` imports.
2. **Domain documentation** — The docstring acts as the canonical one-line description of what the package contains, useful for `help()` introspection and IDE tooltips.

The absence of re-exports is intentional: consumers import directly from the submodule they need (`from ee.cloud.notifications.service import NotificationService`), avoiding a fat `__init__.py` that would couple the router, service, and schema together at import time.

## Integration with the Application Factory

The application factory (typically `ee/cloud/app.py`) imports `ee.cloud.notifications.router` and calls `app.include_router(router)`. The package boundary means the factory only needs to know about the `router` symbol, not the internal service or schema details.

## Known Gaps

- No `__all__` is defined, so `from ee.cloud.notifications import *` would export nothing. This is fine given the direct-import convention but may surprise developers expecting re-exports.
- The domain currently has no background tasks (e.g., expiry enforcement for `expires_at` notifications). A future background task would naturally live in a `tasks.py` module within this package.