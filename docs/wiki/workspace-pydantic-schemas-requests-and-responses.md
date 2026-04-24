---
{
  "title": "Workspace Pydantic Schemas — Requests and Responses",
  "summary": "This module defines the Pydantic request and response schemas for the workspace domain, enforcing input validation at the API boundary before any business logic runs. The slug validator is the key defensive piece: it rejects slugs that would break URL routing or clash with reserved path segments.",
  "concepts": [
    "Pydantic",
    "field_validator",
    "slug validation",
    "request schema",
    "response schema",
    "role enum",
    "invite status",
    "BaseModel",
    "URL safety"
  ],
  "categories": [
    "workspace",
    "schemas",
    "validation",
    "Pydantic",
    "API contract"
  ],
  "source_docs": [
    "8bc0ced486c24d58"
  ],
  "backlinks": null,
  "word_count": 377,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The schemas layer is the contract between the HTTP wire format and the service layer. All incoming data is validated here before it reaches `WorkspaceService`. The split into request schemas and response schemas mirrors the principle that what you accept from a client and what you expose back to a client are separate concerns with separate validation rules.

## Slug Validation

The most non-trivial piece is the `validate_slug` field validator on `CreateWorkspaceRequest`. The regex `^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$` enforces several properties simultaneously:

- All lowercase and alphanumeric — prevents Unicode homograph attacks where two slugs look identical but differ in bytes.
- Hyphens allowed internally but not at the start or end — slugs that start or end with `-` break common URL conventions and can confuse path parsers.
- Single-character slugs are allowed (`^[a-z0-9]$`) — the main clause requires at least two characters to guarantee the leading/trailing rule, so single chars need a separate branch.

```python
@field_validator("slug")
@classmethod
def validate_slug(cls, v: str) -> str:
    if not re.match(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$", v):
        raise ValueError("Slug must be lowercase alphanumeric with hyphens")
    return v
```

## Role Constraints

The `CreateInviteRequest` accepts roles `admin` or `member` (not `owner`) — owners are created only by the service when a workspace is first instantiated. The `UpdateMemberRoleRequest` adds `owner` to the allowed set because an admin can promote someone to owner, but the service layer enforces the invariant that the current owner cannot be demoted via this path.

## Invite Response Shape

The `InviteResponse` includes derived state fields (`accepted`, `revoked`, `expired`) that the frontend uses to determine what actions are available (e.g., whether to show a "resend" button). These are properties on the underlying `Invite` model; surfacing them explicitly here prevents the frontend from needing to implement the same logic.

## Response Schemas vs. Service Dicts

The `WorkspaceResponse`, `MemberResponse`, and `InviteResponse` classes document the exact shape the API returns. In practice the service currently returns plain `dict` objects rather than instantiating these models, but the schema classes serve as documentation and can be used by tests to validate response structure.

## Known Gaps

The service layer returns raw dicts rather than validated response model instances, which means Pydantic cannot catch shape regressions in service output automatically. A future improvement would have the service construct `WorkspaceResponse` objects directly.