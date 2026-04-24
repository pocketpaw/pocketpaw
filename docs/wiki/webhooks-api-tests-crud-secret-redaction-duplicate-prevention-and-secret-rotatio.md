---
{
  "title": "Webhooks API Tests — CRUD, Secret Redaction, Duplicate Prevention, and Secret Rotation",
  "summary": "This test module covers the full lifecycle of PocketPaw's webhook configuration API: listing (with secret redaction), adding (with name validation and duplicate prevention), removing, and regenerating shared secrets. Webhooks enable external services to trigger agent actions, and the tests enforce that secrets are never exposed at rest through the API listing endpoint.",
  "concepts": [
    "webhooks API",
    "secret redaction",
    "HMAC signing secret",
    "webhook name validation",
    "duplicate prevention",
    "secret rotation",
    "GET /api/v1/webhooks",
    "POST /api/v1/webhooks/add",
    "POST /api/v1/webhooks/remove",
    "POST /api/v1/webhooks/regenerate-secret",
    "settings.save",
    "webhook lifecycle"
  ],
  "categories": [
    "testing",
    "API",
    "webhooks",
    "security",
    "test"
  ],
  "source_docs": [
    "c6397caa0116528b"
  ],
  "backlinks": null,
  "word_count": 639,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`test_api_v1_webhooks.py` tests the webhook management endpoints under `/api/v1/webhooks`. PocketPaw webhooks allow external systems to push events into the agent runtime — for example, a CI pipeline notifying the agent of a deployment, or a CRM system sending a new lead. Each webhook has a `name`, a generated `secret` (used for HMAC signature verification), an optional `description`, and a timeout setting.

The test file was created on 2026-02-21 alongside the webhook channel adapter.

## `TestListWebhooks`

**`test_list_webhooks`** is the most security-critical test in the file. It asserts that a webhook's secret (`"abcdef12345678"`) is redacted in the API response and replaced with a value that starts with `"***"`. The raw secret must not appear anywhere in the response.

The reason this matters: the list endpoint is intended for the dashboard to display configured webhooks and their metadata. If the secret were returned in full, any user who can view the dashboard (or any process that can call the API with `settings:read` scope) would have the HMAC signing secret. An attacker with the secret can forge webhook payloads, injecting arbitrary events into the agent runtime as if they came from a trusted external source.

**`test_list_empty`** confirms an empty `webhook_configs` setting returns `{"webhooks": []}` rather than a 404 or null.

## `TestAddWebhook`

**`test_add_webhook`** confirms the happy path: a POST with `{"name": "my-hook", "description": "Test"}` returns HTTP 200, a `status: "ok"`, a webhook entry with the given name, and a generated secret of non-trivial length. Crucially, `mock_s.save.assert_called_once()` verifies that the new webhook is persisted to the settings file. Without the save call, the webhook would exist in memory only and disappear on restart.

**`test_add_missing_name`** — empty name string returns 400. A nameless webhook cannot be referenced in routing rules or removed later.

**`test_add_invalid_name`** — `"bad name!"` (contains space and exclamation mark) returns 400. Webhook names are used as URL path components (`/webhook/<name>`) and potentially as filesystem keys; special characters would break routing or enable path injection.

**`test_add_duplicate`** — if `"existing"` is already in `webhook_configs`, trying to add another `"existing"` returns HTTP 409 Conflict. Without this guard, duplicate names would create ambiguous routing (two handlers competing for the same URL prefix) and silent config corruption.

## `TestRemoveWebhook`

**`test_remove_existing`** confirms that removing a webhook that is in `webhook_configs` returns 200 and calls `settings.save()`. The save assertion is as important here as in add: an unsaved removal would cause the deleted webhook to reappear on restart.

**`test_remove_nonexistent`** — attempting to remove a webhook name not in the list returns 404 rather than silently succeeding. This lets callers detect typos or race conditions (another client already removed it).

## `TestRegenerateSecret`

**`test_regenerate`** validates the secret rotation flow: POST with `{"name": "my-hook"}` returns 200 with a new secret that is different from the old one. Secret rotation is needed when a secret is compromised or when rotating as a routine security practice. The test asserts `data["secret"] != old_secret` — a weak but necessary check. (The probability of accidentally generating the same secret is negligible for a sufficiently long random value.)

**`test_regenerate_not_found`** — returns 404 for an unknown webhook name, preventing silent no-ops.

## Known Gaps

- **Secret strength** — `test_add_webhook` only checks `len(secret) > 10`. There is no assertion on entropy, character set, or minimum bit strength. A weak RNG or a short secret would pass this test.
- **Delivery testing** — the test suite covers webhook CRUD but has no tests for the actual webhook *delivery* path (receiving an HTTP POST, verifying the HMAC signature, and dispatching to the agent). Those tests presumably live in the channel adapter's own test module.
- **Timeout validation** — `webhook_sync_timeout` appears in the list response fixture but is not tested for acceptable range (e.g., rejecting 0 or negative values).
- **Name length cap** — there is no test preventing pathologically long webhook names that could overflow database columns or filesystem path limits.
