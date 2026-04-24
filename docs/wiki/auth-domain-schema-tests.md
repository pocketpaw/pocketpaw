---
{
  "title": "Auth Domain Schema Tests",
  "summary": "This module tests the Pydantic schemas for the auth domain — `ProfileUpdateRequest`, `SetWorkspaceRequest`, and `UserResponse` — verifying optional field defaults, value round-trips, and the structure of the user identity response.",
  "concepts": [
    "ProfileUpdateRequest",
    "SetWorkspaceRequest",
    "UserResponse",
    "Pydantic",
    "auth schemas",
    "partial update",
    "PATCH",
    "active workspace",
    "email verification"
  ],
  "categories": [
    "auth",
    "schemas",
    "testing",
    "test"
  ],
  "source_docs": [
    "06d4327e954a0b41"
  ],
  "backlinks": null,
  "word_count": 509,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `tests/cloud/test_auth_schemas.py` module provides lightweight schema-level tests for the authentication domain. Auth schemas are the boundary between HTTP request bodies and the service layer — getting the field optionality and types wrong here causes either overly strict APIs (rejecting valid requests) or overly permissive ones (accepting malformed data).

## ProfileUpdateRequest

`ProfileUpdateRequest` is a PATCH-style schema for user profile updates. All fields (`full_name`, `avatar`, `status`) are optional, allowing partial updates. `test_profile_update_optional_fields` confirms that instantiating with no arguments yields `None` for all three fields — this is the correct default for a PATCH schema where absence means "do not change."

`test_profile_update_with_values` confirms that provided values are stored correctly:

```python
body = ProfileUpdateRequest(full_name="Rohit", avatar="https://example.com/img.png")
assert body.full_name == "Rohit"
```

This prevents a regression where Pydantic field aliases or validators silently transform the input.

## SetWorkspaceRequest

`SetWorkspaceRequest` contains a single required field: `workspace_id`. The test confirms it accepts and stores the ID string correctly. This schema is used when a user switches their active workspace context — the correct workspace ID must flow through to the session and all subsequent requests.

## UserResponse

`UserResponse` is the serialized identity returned after authentication. The test constructs a full instance with all required fields:
- `id`, `email`, `name`, `image`, `email_verified`
- `active_workspace` (set to `None` for a user who has not yet selected a workspace)
- `workspaces` (empty list for a new user)

The assertion checks `resp.email == "a@b.com"`, confirming the schema does not mangle the email. The `active_workspace=None` case is important: new users who have not yet created a workspace must receive a valid response, not a serialization error.

## Why These Tests Exist

Auth schemas are touched by nearly every developer adding new features. A seemingly innocuous change — adding a validator, renaming a field, changing optionality — can break authentication flows for all clients. These tests are a quick regression net that catches such changes without requiring a running server.

## Schema Stability and API Evolution

The auth schemas sit at a critical junction: they define the contract between the frontend and the backend for user identity. Because authentication flows are exercised on every page load and every API call, even a small schema change (e.g., making `email_verified` optional, or adding a required `plan` field) can break frontend code that reads these fields. The tests here provide a low-cost smoke check that the schema remains structurally correct without requiring a running authentication service or a real database. They complement the API contract tests that verify the actual HTTP response shapes at the integration level.

In future iterations, these tests could be extended with parametrized inputs to cover edge cases like very long names, Unicode characters in display names, and workspace IDs with special characters — all scenarios that have historically caused serialization or database-write failures in similar systems.

## Known Gaps

No TODO or FIXME markers. The tests do not cover validation errors (e.g., invalid email format for `UserResponse`, excessively long `full_name`). The `status` field on `ProfileUpdateRequest` is not tested with a value. Multi-workspace scenarios (non-empty `workspaces` list) are not covered.