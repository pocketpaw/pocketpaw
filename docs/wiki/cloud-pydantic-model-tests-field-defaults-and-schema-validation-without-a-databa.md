---
{
  "title": "Cloud Pydantic Model Tests: Field Defaults and Schema Validation Without a Database",
  "summary": "This test file verifies field-level Pydantic model behavior for core cloud entities (Group, Message, Pocket, Invite, Workspace, Session, Notification) using `model_construct()` to bypass Beanie's MongoDB-dependent `__init__`. It pins default values, optional field nullability, and enum acceptance for fields added in recent schema migrations.",
  "concepts": [
    "Pydantic model_construct",
    "Beanie ODM",
    "schema testing",
    "soft delete",
    "field defaults",
    "Group model",
    "Pocket model",
    "Workspace model",
    "Session model",
    "Notification model",
    "Invite model",
    "visibility enum"
  ],
  "categories": [
    "testing",
    "data models",
    "schema validation",
    "test"
  ],
  "source_docs": [
    "858c16b3ec491189"
  ],
  "backlinks": null,
  "word_count": 511,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's cloud layer uses Beanie (an async MongoDB ODM) for its document models. Beanie's `Document` subclass overrides `__init__` to require a live MongoDB collection, making it impossible to instantiate models in unit tests without a database connection. This test file works around that limitation using Pydantic's `model_construct()`, which bypasses `__init__` entirely and creates instances directly from field values.

## Why `model_construct()` Instead of the Normal Constructor

Using `model_construct()` has two benefits:

1. **No database required** — Tests run without MongoDB, making them fast and suitable for CI environments without infrastructure.
2. **Tests defaults directly** — `model_construct()` applies Pydantic field defaults without going through validators, so a field initialized with `default=None` will be `None` on the constructed instance. This is exactly what these tests need: verifying that newly added fields have sane defaults.

The tradeoff is that `model_construct()` skips Pydantic validators. These tests are not checking validation — they're checking schema shape and defaults. Validation is tested separately via endpoint or schema tests.

## Fields Under Test

**Group** — `type` field accepts `"dm"` (direct message) as a valid value. `last_message_at` defaults to `None`. `message_count` defaults to `0`. These fields were added when DM support was introduced; the tests prevent regressions where a missing field or wrong default would silently break DM channel behavior.

**Message** — `edited_at` defaults to `None`. This field tracks when a message was last edited. If it defaulted to the current timestamp instead of `None`, unedited messages would appear as edited.

**Pocket** — `share_link_token` defaults to `None`, `share_link_access` defaults to `"view"`, `visibility` defaults to `"workspace"`, `shared_with` defaults to `[]`. These are the sharing-model fields. The `"view"` default for `share_link_access` is the least-permissive option, preventing accidental edit access when a share link is generated.

**Pocket visibility** — `"private"`, `"workspace"`, and `"public"` are all valid values. `test_pocket_visibility_values` iterates all three to confirm the field accepts the full enum.

**Invite** — `revoked` defaults to `False`. An invite is active by default; it must be explicitly revoked. A default of `True` would mean all invites start as revoked, breaking the invite flow.

**Workspace** — `deleted_at` defaults to `None`. This supports soft deletes: a workspace is deleted by setting `deleted_at` to a timestamp, not by removing the document.

**Session** — `deleted_at` defaults to `None`. Same soft-delete pattern as Workspace.

**Notification** — `expires_at` defaults to `None`. Notifications without an explicit TTL never auto-expire; expiry must be explicitly set.

## Pattern Value

This test file acts as a living schema changelog. Every time a new field is added to a cloud model, a corresponding test should be added here to document the field's name, type, and default. This makes it easy for future developers to understand what was added and why.

## Known Gaps

- **Validation not tested here** — Since `model_construct()` bypasses validators, validation logic (e.g., visibility must be one of the allowed values) is not covered. This must be tested at the schema or endpoint layer.
- **Beanie-specific metadata** — Fields like `id` (Beanie's `PydanticObjectId`) may behave differently when constructed vs. loaded from MongoDB. These tests don't cover round-trip serialization through Beanie.
