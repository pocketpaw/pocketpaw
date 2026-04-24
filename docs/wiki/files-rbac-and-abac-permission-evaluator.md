---
{
  "title": "Files RBAC and ABAC Permission Evaluator",
  "summary": "Implements the two-layer permission model for the files module: RBAC permissions come from providers on a per-entry basis, while ABAC rules apply as a post-filter that can only further restrict visibility. The `PermissionsEvaluator` combines both layers to produce the final UI-facing capability list for each file entry.",
  "concepts": [
    "RBAC",
    "ABAC",
    "PermissionsEvaluator",
    "Permission",
    "derive_capabilities",
    "capability filtering",
    "baseline_rbac",
    "abac_config",
    "RequestContext",
    "FileEntry",
    "access control",
    "mount_writable"
  ],
  "categories": [
    "files",
    "security",
    "permissions",
    "cloud"
  ],
  "source_docs": [
    "020101810eedc848"
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

The `ee.cloud.files.permissions` module enforces a hybrid permission model that separates provider-declared capabilities from policy-level access control. This separation is deliberate: it means neither layer can grant more access than the other intends to allow.

## Two-Layer Architecture

### Layer 1: RBAC (Role-Based Access Control)

RBAC permissions originate in the provider. Each provider implements `baseline_rbac(ctx, entry) -> Permission`, returning a `Permission(read, write, manage)` tuple based on the authenticated user's relationship to the entry (owner, admin, member, etc.). This layer is trust-authoritative: only the provider that owns the entry knows the correct role.

### Layer 2: ABAC (Attribute-Based Access Control)

ABAC rules are a post-filter loaded from `abac_config`. They evaluate entry and context attributes (workspace tags, file types, user attributes) and return a boolean `abac_allowed` flag. ABAC can only restrict -- it cannot grant permissions the RBAC layer did not already provide. This prevents configuration mistakes in ABAC rules from accidentally elevating access.

## derive_capabilities Logic

The final capability set is derived by AND-ing both layers:

- `read` / `download` -- requires `rbac.read AND abac_allowed`
- `rename` / `move` / `replace` / `upload` -- requires `rbac.write AND mount_writable AND abac_allowed`
- `delete` -- requires `rbac.manage AND mount_writable AND abac_allowed`

Critically, only capabilities the provider already declared on the entry survive. If the provider set `capabilities=["read", "download"]` on a `FileEntry`, the evaluator cannot add `rename` even if RBAC and ABAC would permit it. This lets providers opt out of capabilities at the entry level -- for example, a Knowledge Base provider might never expose `delete` regardless of the user's role.

## PermissionsEvaluator.filter

```python
def filter(self, *, entries: list[FileEntry], ctx: RequestContext) -> list[FileEntry]
```

The `filter` method takes a list of raw entries from an aggregated provider query and returns only those the current user is allowed to see, with each entry's `capabilities` list narrowed to what's actually permitted. Entries where `rbac.read` or `abac_allowed` is false are dropped entirely -- they do not appear in the API response at all, preventing enumeration of forbidden files.

## Why Combine RBAC and ABAC?

RBAC alone cannot express policies like "users in workspace X cannot download files tagged 'confidential' unless they have the 'data-steward' attribute." ABAC alone would require replicating all ownership logic into attribute rules, which is brittle. The combination keeps ownership logic in providers (who own the data) and policy logic in ABAC config (who manages policy), matching organisational responsibilities.

## Known Gaps

- **No audit log.** Permission denials are silent -- there is no structured log of why an entry was filtered out. This makes debugging permission issues difficult in production.
- **ABAC config hot-reload is unclear.** The module imports `abac_config` at load time; if rules change, whether they take effect without restart depends on how `abac_config` is structured, which is not documented here.