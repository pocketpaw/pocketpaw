---
{
  "title": "Workspace Schema Tests: Slug Validation, Invite Rules, and Member Role Contracts",
  "summary": "Tests for the workspace domain Pydantic schemas in PocketPaw's EE layer, covering slug format enforcement, invite role validation, and member role update rules. The slug tests are particularly important because workspace slugs appear in URLs and must be lowercase, hyphen-separated, and free of leading/trailing hyphens.",
  "concepts": [
    "CreateWorkspaceRequest",
    "UpdateWorkspaceRequest",
    "CreateInviteRequest",
    "UpdateMemberRoleRequest",
    "slug validation",
    "workspace role",
    "member invite",
    "Pydantic validation",
    "URL slug",
    "multi-tenant"
  ],
  "categories": [
    "testing",
    "schemas",
    "workspace",
    "cloud API",
    "test"
  ],
  "source_docs": [
    "b54e245179c43829"
  ],
  "backlinks": null,
  "word_count": 389,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Workspaces are the top-level organizational unit in PocketPaw's multi-tenant cloud. A workspace has a unique slug used in URLs and API paths. The `ee.cloud.workspace.schemas` module provides `CreateWorkspaceRequest`, `UpdateWorkspaceRequest`, `CreateInviteRequest`, and `UpdateMemberRoleRequest`. This test file validates the schema-level rules that prevent malformed data from reaching the service layer.

## Slug Validation

The slug is the most constrained field. `test_create_workspace_slug_validation` shows that spaces, punctuation, and special characters are rejected with `PydanticValidationError`. Individual edge cases are tested separately:

- `test_create_workspace_slug_no_leading_hyphen` — prevents slugs like `-bad` which would produce URLs starting with a double-hyphen
- `test_create_workspace_slug_no_trailing_hyphen` — prevents `bad-` which looks broken to users
- `test_create_workspace_slug_no_uppercase` — enforces lowercase-only so URLs are case-insensitively unique
- `test_create_workspace_single_char_slug` — confirms minimum length is one character (not zero)

These tests exist because URL slugs double as database keys and API path segments. A slug with uppercase letters would let two workspaces share the same effective slug (`Acme` vs `acme`), breaking the uniqueness guarantee. Leading/trailing hyphens would produce ugly and hard-to-copy URLs.

## Empty Field Rejection

`test_create_workspace_empty_name_rejected` and `test_create_workspace_empty_slug_rejected` verify that Pydantic's `min_length` or `validator` constraints reject empty strings. Without these, a workspace could be created with no visible name or a blank URL segment, breaking listings and routing.

## UpdateWorkspaceRequest

All fields are optional (PATCH semantics). `test_update_workspace_all_optional` confirms zero-argument construction succeeds and all fields are `None`. `test_update_workspace_with_values` confirms that a `settings` dict round-trips as-is — settings are stored as an opaque map, so any dict must be accepted.

## CreateInviteRequest

Invites have a `role` field constrained to known values. `test_create_invite_defaults` shows the default is `"member"` — the least-privileged role. `test_create_invite_admin_role` and `test_create_invite_role_validation` confirm that `"admin"` is valid but an unknown role like `"superadmin"` raises `PydanticValidationError`. This prevents a client bug (or malicious input) from granting an unrecognized role that might be interpreted differently by the backend.

`test_create_invite_with_group` confirms the optional `group_id` field for scoped group membership invitations.

## UpdateMemberRoleRequest

Member roles can be promoted up to `"owner"`. `test_update_member_role_owner` verifies `"owner"` is accepted. `test_update_member_role_invalid` confirms `"superadmin"` is rejected, preventing the same role-inflation risk as with invites.

## Known Gaps

There is no test for the maximum slug length. There are no tests for Unicode slugs or internationalized workspace names (though the schema likely accepts them). The `settings` map is accepted as any dict — there are no schema-level constraints on what keys it may contain.
