---
{
  "title": "Notifications REST API Router",
  "summary": "The notifications router exposes three endpoints for listing, marking read, and bulk-clearing a user's in-app notifications, with authentication enforced via `current_active_user` dependency injection. All mutation operations are scoped strictly to the authenticated user to prevent cross-user data access.",
  "concepts": [
    "FastAPI router",
    "notifications",
    "current_active_user",
    "idempotency",
    "bulk update",
    "authentication dependency",
    "query parameters",
    "cross-user protection"
  ],
  "categories": [
    "notifications",
    "api",
    "enterprise-cloud"
  ],
  "source_docs": [
    "929a89508f681934"
  ],
  "backlinks": null,
  "word_count": 470,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The notifications router is a thin FastAPI `APIRouter` that translates HTTP requests into `NotificationService` calls. It deliberately contains no business logic: all reads and writes flow through the service layer, keeping the router focused on HTTP concerns (authentication, parameter extraction, response shaping).

## Endpoints

### `GET /notifications`

Lists notifications for the authenticated user with two optional query parameters:

- `unread: bool = False` — when `True`, returns only unread notifications. This powers the notification badge and unread-only filter in the frontend.
- `limit: int = 50` — bounded between 1 and 200 by `ge=1, le=200` constraints. The upper bound of 200 prevents accidental full-dump queries from a misconfigured client; the default of 50 is enough for the visible notification panel.

The return type is `list[dict]` rather than `list[NotificationResponse]`. The service's `_to_wire()` function serializes the Beanie document to a dict, and the router passes it through. This approach works but loses FastAPI's response validation and OpenAPI schema generation for this endpoint.

### `POST /notifications/{notification_id}/read`

Marks a single notification as read. The service validates that the notification belongs to the authenticated user before mutating it — a user cannot mark another user's notification as read even if they know the ObjectId. The endpoint returns `{"ok": True}` on success.

The service's idempotency check (`if notif.read: return`) means calling this endpoint twice on an already-read notification is a no-op rather than an error. This matters for clients that may retry on network failure.

### `POST /notifications/clear`

Bulk-marks all unread notifications as read for the authenticated user. Returns `{"cleared": count}` where count is the number of documents updated. The bulk update path in `NotificationService.clear_all` uses MongoDB's `update_many` rather than iterating and saving individually, which is significantly faster for users with large notification backlogs.

## Authentication Pattern

All three endpoints use `Depends(current_active_user)` from `ee.cloud.auth`. This dependency resolves the JWT/session cookie to a `User` object and raises `401 Unauthorized` if the token is missing or invalid. The user object is the sole source of the `user_id` passed to the service — the router never accepts a `user_id` from the request body or path parameters, preventing impersonation attacks.

## Failure Scenarios Prevented

- **Cross-user read**: `mark_read` validates `notif.recipient == user_id` before saving, so a user with a valid JWT cannot mark another user's notification read.
- **Unbounded list**: the `le=200` limit cap prevents accidental full-collection reads from a single API call.
- **Double-read errors**: idempotent `mark_read` means retry-on-failure clients do not trigger 4xx errors.

## Known Gaps

- The `GET /notifications` endpoint returns `list[dict]` instead of `list[NotificationResponse]`, losing OpenAPI schema generation and response validation for this endpoint.
- No `DELETE /notifications/{id}` endpoint — individual notification deletion is not supported; users can only bulk-clear.
- No pagination cursor — the `limit` parameter caps results but there is no `offset` or cursor for paginating through large notification histories.