---
{
  "title": "Webhook Routes Test Suite: Dashboard Auth, HMAC Validation, and Slot Management",
  "summary": "This module tests the webhook dashboard routes in PocketPaw, covering Bearer token authentication, HMAC-based inbound secret validation, slot CRUD via the dashboard API, and async dispatch through the webhook adapter. It ensures that unauthenticated requests are rejected and that slot configuration persists correctly.",
  "concepts": [
    "webhook dashboard",
    "Bearer authentication",
    "HMAC secret",
    "slot CRUD",
    "WebhookAdapter",
    "_channel_adapters",
    "Settings.load",
    "async dispatch",
    "dashboard routes",
    "FastAPI",
    "TestClient"
  ],
  "categories": [
    "testing",
    "webhook",
    "dashboard",
    "authentication",
    "API routes",
    "test"
  ],
  "source_docs": [
    "f5345eacf5df143d"
  ],
  "backlinks": null,
  "word_count": 447,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`tests/test_webhook_routes.py` validates the FastAPI routes that manage webhook slots through PocketPaw's dashboard. Created on 2026-02-09, it tests authentication, secret validation, slot creation/deletion, and async dispatch. Three autouse fixtures wire up the mock environment for every test.

## Fixture Architecture

Three `autouse` fixtures run for every test class:

- **`_mock_settings`**: Patches `Settings.load()` to return a mock with one pre-existing webhook slot. This prevents real disk I/O and `.env` reads during tests.
- **`_mock_token`**: Patches `get_access_token` to return a fixed token. This allows the dashboard Bearer auth middleware to pass without a running auth server.
- **`_mock_adapter`**: Injects a mock `WebhookAdapter` into the `_channel_adapters` registry and cleans it up after each test. Without this, tests that call the inbound webhook URL would fail because no adapter is registered.

The `_auth_headers()` helper builds request headers with the Bearer token plus any extra headers, keeping test code clean.

## Authentication Tests (`TestWebhookAuth`)

The dashboard uses Bearer token authentication on all `/api/*` routes. Tests verify:

- A valid `Authorization: Bearer <token>` header gets a 200 from the inbound webhook URL.
- A missing or wrong token gets a 401, preventing unauthenticated writes to the webhook dispatch path.

This matters because the inbound webhook URL is the entry point for external systems. Without auth, any internet client could inject messages into the agent.

## HMAC Secret Validation

Inbound webhook requests must include a secret header. Tests verify:

- A matching secret header is accepted.
- A missing or mismatched secret is rejected with a 403.

HMAC or simple secret comparison prevents replay attacks and ensures only the configured external system (e.g., GitHub, Stripe) can trigger the agent.

## Slot Management Tests

The dashboard exposes CRUD for webhook slots:

- **List**: `GET /api/v1/webhooks` returns the current slot list from settings.
- **Create**: `POST /api/v1/webhooks` adds a new slot, calls `settings.save()`, and returns the created slot.
- **Delete**: `DELETE /api/v1/webhooks/{name}` removes a slot by name and persists the change.

The `settings.save` mock is asserted after write operations, ensuring persistence is not silently skipped.

## Async Dispatch

When a valid inbound request arrives, the route calls `adapter.handle_webhook(slot, body, request_id, sync=False)`. Tests assert:

- The adapter's `handle_webhook` is called with the correct slot and parsed body.
- A 200 is returned immediately in async mode (fire-and-forget).

## Known Gaps

- Sync mode dispatch (where the HTTP response waits for the agent reply) is not covered here; it is tested in `test_webhook_adapter.py`.
- No test verifies that slot names are validated for uniqueness on creation—a duplicate slot name could silently overwrite an existing one.
- The HMAC comparison tests use a simple string equality check in fixtures; no test verifies timing-safe comparison to prevent timing attacks.
